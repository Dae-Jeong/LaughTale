"""E3: bounded ramp across one preconditioned Fanout 1→2 scale operation."""

import argparse
import asyncio
import json
import struct
import subprocess
import time
from collections import Counter
from contextlib import AsyncExitStack
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

from fanout_oracle import INITIAL_APPS, compare, role_transition, stable_owners
from growth import entry_request, kube_command, metrics_check, now, resource_snapshot
from growth_oracle import timestamp
from oracle import ACTOR, ROOM, EvidenceError, normalize
from runner import MESSAGE_PATH, LabHTTP, append, open_peer, wait_receipts, write_new

GROUP = "chat-fanout-v1"
TOPIC = "chat.message-created.v1"
BOOTSTRAP = "127.0.0.1:19092"


def assignment(raw):
    """Decode the bounded Kafka consumer assignment wire data, not app events."""
    if not raw:
        return []
    if len(raw) > 16384:
        raise EvidenceError("assignment_too_large")
    cursor = 0

    def integer(size):
        nonlocal cursor
        value = struct.unpack_from(">h" if size == 2 else ">i", raw, cursor)[0]
        cursor += size
        return value

    if integer(2) != 0:
        raise EvidenceError("assignment_version_unsupported")
    count = integer(4)
    if count not in (0, 1):
        raise EvidenceError("assignment_topic_count_invalid")
    result = []
    for _ in range(count):
        length = integer(2)
        if not 0 < length <= 256:
            raise EvidenceError("assignment_topic_length_invalid")
        topic = raw[cursor : cursor + length].decode()
        cursor += length
        if topic != TOPIC:
            raise EvidenceError("assignment_topic_unexpected")
        partitions = integer(4)
        if not 0 <= partitions <= 4:
            raise EvidenceError("assignment_partition_count_invalid")
        result.extend(integer(4) for _ in range(partitions))
    user_length = integer(4)
    if user_length < -1 or cursor + max(0, user_length) != len(raw):
        raise EvidenceError("assignment_trailing_data")
    if len(set(result)) != len(result) or any(p not in range(4) for p in result):
        raise EvidenceError("assignment_partitions_invalid")
    return result


def group_description(responses):
    groups = [group for response in responses for group in response.groups]
    if len(groups) != 1:
        raise EvidenceError("group_description_count_invalid")
    error, group, state, protocol_type, _, members = groups[0][:6]
    if error or group != GROUP or protocol_type != "consumer" or len(members) > 2:
        raise EvidenceError("unexpected_consumer_group")
    normalized = []
    for member in members:
        member_id, client_id, _, _, raw_assignment = member
        prefix = "chat-fanout-"
        if not client_id.startswith(prefix):
            raise EvidenceError("fanout_pod_client_id_required")
        uid = client_id[len(prefix) :]
        if str(UUID(uid)) != uid:
            raise EvidenceError("fanout_client_id_uid_invalid")
        normalized.append(
            {
                "member_id": member_id,
                "client_id": client_id,
                "pod_uid": uid,
                "partitions": assignment(raw_assignment),
            }
        )
    return state, normalized


class KafkaObserver:
    async def start(self):
        from aiokafka import AIOKafkaConsumer, TopicPartition
        from aiokafka.admin import AIOKafkaAdminClient

        self.admin = AIOKafkaAdminClient(
            bootstrap_servers=BOOTSTRAP,
            client_id="e3-read-only-admin",
            request_timeout_ms=3000,
        )
        self.consumer = AIOKafkaConsumer(
            bootstrap_servers=BOOTSTRAP,
            group_id=None,
            enable_auto_commit=False,
            auto_offset_reset="latest",
            request_timeout_ms=3000,
            max_partition_fetch_bytes=65536,
            fetch_max_bytes=262144,
        )
        await self.admin.start()
        await self.consumer.start()
        if TOPIC not in await self.consumer.topics():
            raise EvidenceError("topic_missing")
        self.partitions = [TopicPartition(TOPIC, p) for p in range(4)]
        self.consumer.assign(self.partitions)
        self.beginnings = await self.consumer.beginning_offsets(self.partitions)
        if self.consumer.partitions_for_topic(TOPIC) != set(range(4)):
            raise EvidenceError("four_partitions_required")
        ends = await self.consumer.end_offsets(self.partitions)
        for partition in self.partitions:
            self.consumer.seek(partition, ends[partition])

    async def snapshot(self):
        async with asyncio.timeout(5):
            state, members = group_description(
                await self.admin.describe_consumer_groups([GROUP])
            )
            committed = await self.admin.list_consumer_group_offsets(
                GROUP, partitions=self.partitions
            )
            ends = await self.consumer.end_offsets(self.partitions)
        offsets = []
        for partition in self.partitions:
            raw = committed[partition].offset
            value = (
                0
                if raw == -1 and self.beginnings[partition] == ends[partition] == 0
                else raw
            )
            offsets.append(
                {
                    "partition": partition.partition,
                    "raw_committed": raw,
                    "committed": value,
                    "end": ends[partition],
                    "lag": ends[partition] - value,
                }
            )
        return {
            "observed_at": now(),
            "group": GROUP,
            "state": state,
            "members": members,
            "offsets": offsets,
        }

    async def close(self):
        async with asyncio.timeout(5):
            if hasattr(self, "consumer"):
                await self.consumer.stop()
            if hasattr(self, "admin"):
                await self.admin.close()


async def deployment():
    raw = json.loads(await kube_command("get", "deployment/chat-fanout", "-o", "json"))
    return {
        "uid": raw["metadata"]["uid"],
        "resource_version": raw["metadata"]["resourceVersion"],
        "replicas": raw["spec"]["replicas"],
        "ready": raw["status"].get("readyReplicas", 0),
        "observed_generation": raw["status"].get("observedGeneration", 0),
        "generation": raw["metadata"]["generation"],
    }


async def scale_once(evidence, run_dir, start):
    await asyncio.sleep(max(0, start + 10 - time.monotonic()))
    try:
        before = await deployment()
        if (
            before["replicas"] != 1
            or before["ready"] != 1
            or before["uid"] != evidence["initial_deployment"]["uid"]
            or (run_dir / "STOP").exists()
        ):
            raise EvidenceError("scale_preconditions_failed")
        evidence["scale"] = {
            "before": before,
            "deployment_uid": before["uid"],
            "resource_version_before": before["resource_version"],
            "requested_at": now(),
            "writes": 1,
            "success": False,
        }
        write_new(run_dir / "scale-request.json", evidence["scale"])
        await kube_command(
            "scale",
            "deployment/chat-fanout",
            "--replicas=2",
            "--current-replicas=1",
            "--resource-version=" + before["resource_version"],
        )
        evidence["scale"].update(success=True, completed_at=now())
        write_new(run_dir / "scale-result.json", evidence["scale"])
        for _ in range(10):
            state = await deployment()
            if (
                state["uid"] != before["uid"]
                or state["replicas"] != 2
                or (run_dir / "STOP").exists()
            ):
                raise EvidenceError("unexpected_rollout_state")
            if (
                state["ready"] == 2
                and state["observed_generation"] >= state["generation"]
            ):
                evidence["scale"]["rollout_ready_observed_at"] = now()
                return
            await asyncio.sleep(2)
        raise EvidenceError("rollout_timeout")
    except (ValueError, OSError, KeyError, TimeoutError):
        evidence.setdefault("scale", {})["error"] = "scale_or_rollout_unknown_no_retry"
        (run_dir / "STOP").touch(exist_ok=True)


async def observe_kafka(observer, evidence, run_dir, done):
    from aiokafka.errors import KafkaError

    while True:
        try:
            await asyncio.wait_for(done.wait(), 1)
        except TimeoutError:
            pass
        try:
            sample = await observer.snapshot()
            evidence["kafka"].append(sample)
            append(run_dir / "kafka.jsonl", sample)
            if len(evidence["kafka"]) > 120:
                raise EvidenceError("kafka_sample_limit")
        except (KafkaError, ValueError, KeyError, OSError, TimeoutError, struct.error):
            evidence["kafka_error"] = "required_group_sample_failed"
            (run_dir / "STOP").touch(exist_ok=True)
            return
        if done.is_set():
            evidence["kafka_monitoring_completed"] = True
            return


async def observe_resources(evidence, run_dir, done):
    baseline = evidence["samples"][0]
    old = {row["uid"] for row in baseline["pods"]}
    added = set()
    while True:
        try:
            await asyncio.wait_for(done.wait(), 5)
        except TimeoutError:
            pass
        try:
            sample = await asyncio.to_thread(resource_snapshot)
            errors = role_transition(
                baseline, sample, "chat-fanout", bool(evidence.get("scale"))
            )
            added.update(row["uid"] for row in sample["pods"] if row["uid"] not in old)
            if len(added) > 1:
                errors.append("more_than_one_new_fanout")
            sample["transition_issues"] = errors
            evidence["samples"].append(sample)
            append(run_dir / "resources.jsonl", sample)
            if errors:
                raise EvidenceError("resource_transition_invalid")
        except (ValueError, KeyError, OSError, subprocess.SubprocessError):
            evidence["resource_error"] = "resource_guard_failed"
            (run_dir / "STOP").touch(exist_ok=True)
            return
        if done.is_set():
            evidence["monitoring_completed"] = True
            return


def schedule():
    return (
        [(float(index), 1) for index in range(10)]
        + [(10 + index / 5, 5) for index in range(50)]
        + [(20 + index / 10, 10) for index in range(150)]
    )


async def exercise(evidence, run_dir):
    if (
        await kube_command(
            "auth", "can-i", "update", "deployments/chat-fanout", "--subresource=scale"
        )
    ).strip() != "yes":
        raise EvidenceError("scale_permission_denied")
    initial = await deployment()
    if initial["replicas"] != 1 or initial["ready"] != 1:
        raise EvidenceError("initial_fanout_not_one_ready")
    evidence["initial_deployment"] = initial
    baseline = await asyncio.to_thread(resource_snapshot)
    if Counter(
        row["app"] for row in baseline["pods"]
    ) != INITIAL_APPS or role_transition(baseline, baseline, "chat-fanout", False):
        raise EvidenceError("initial_topology_invalid")
    evidence["samples"].append(baseline)
    append(run_dir / "resources.jsonl", baseline)
    evidence["initial_db_metrics"] = await asyncio.to_thread(metrics_check)
    observer = KafkaObserver()
    try:
        await observer.start()
        first = await observer.snapshot()
        evidence["kafka"].append(first)
        append(run_dir / "kafka.jsonl", first)
        fanout_uids = {
            row["uid"] for row in baseline["pods"] if row["app"] == "chat-fanout"
        }
        if (
            len(first["members"]) != 1
            or set((stable_owners(first) or {}).values()) != fanout_uids
            or any(row["lag"] != 0 for row in first["offsets"])
        ):
            raise EvidenceError("initial_stable_consumer_or_zero_lag_missing")
        http = LabHTTP()
        status, data, cookie = await http.request(
            "api_a", "/v1/dev/session", method="POST", body={"user": "user_a"}
        )
        if status != 200 or not cookie or data["data"]["user_id"] != ACTOR:
            raise EvidenceError("session_issue_failed")
        status, data, _ = await asyncio.to_thread(entry_request, "/v1/session", cookie)
        if status != 200 or data["data"]["user_id"] != ACTOR:
            raise EvidenceError("entry_session_failed")
        evidence["session_entry_verified"] = True
        done = asyncio.Event()
        monitoring = [
            asyncio.create_task(observe_resources(evidence, run_dir, done)),
            asyncio.create_task(observe_kafka(observer, evidence, run_dir, done)),
        ]
        scaling = None
        try:
            async with AsyncExitStack() as stack:
                for index, target in enumerate(("gateway_a", "gateway_b")):
                    await open_peer(
                        stack, target, cookie, f"peer-{index}", evidence, run_dir
                    )
                start = time.monotonic()
                evidence["traffic_started_at"] = now()
                scaling = asyncio.create_task(scale_once(evidence, run_dir, start))
                for index, intent in enumerate(evidence["plan"]):
                    await asyncio.sleep(
                        max(0, start + intent["scheduled_seconds"] - time.monotonic())
                    )
                    if (run_dir / "STOP").exists():
                        raise EvidenceError("operator_or_guard_stop")
                    sent = time.monotonic()
                    row = {
                        **intent,
                        "attempt_id": index,
                        "outcome": "unknown",
                        "ack_status": None,
                        "started_at": now(),
                        "generator_lag_seconds": max(
                            0, sent - start - intent["scheduled_seconds"]
                        ),
                    }
                    try:
                        status, data, uid = await asyncio.to_thread(
                            entry_request,
                            MESSAGE_PATH,
                            cookie,
                            {
                                "client_message_id": intent["client_message_id"],
                                "text": f"fanout-synthetic:{evidence['run_id']}:{index}",
                            },
                        )
                        row.update(http_status=status, pod_uid=uid)
                        if status in (200, 201):
                            message = normalize(data["data"])
                            row.update(
                                outcome="acknowledged",
                                ack_status=status,
                                message=message,
                                response_message_id=message["message_id"],
                                response_seq=message["seq"],
                            )
                        elif 400 <= status < 500:
                            row["outcome"] = "rejected"
                    except (OSError, ValueError, KeyError, TypeError):
                        row["reason"] = "transport_or_response_unknown"
                    row.update(
                        completed_at=now(), latency_seconds=time.monotonic() - sent
                    )
                    evidence["attempts"].append(row)
                    append(run_dir / "requests.jsonl", row)
                    if row["outcome"] != "acknowledged":
                        (run_dir / "STOP").touch(exist_ok=True)
                        raise EvidenceError("request_not_acknowledged")
                await wait_receipts(evidence)
                await scaling
                for _ in range(10):
                    if (run_dir / "STOP").exists():
                        raise EvidenceError("experiment_stopped")
                    last = evidence["kafka"][-1]
                    if (
                        len(last["members"]) == 2
                        and stable_owners(last)
                        and all(row["lag"] == 0 for row in last["offsets"])
                        and timestamp(last["observed_at"])
                        >= timestamp(evidence["attempts"][-1]["completed_at"])
                    ):
                        break
                    await asyncio.sleep(1)
                for peer in evidence["peers"]:
                    peer["alive_after_traffic"] = not peer["closed"].is_set()
        finally:
            done.set()
            if asyncio.current_task().cancelling():
                for task in monitoring:
                    task.cancel()
            if scaling is not None and not scaling.done():
                scaling.cancel()
            await asyncio.gather(
                *monitoring, *([scaling] if scaling else []), return_exceptions=True
            )
        evidence["final_db_metrics"] = await asyncio.to_thread(metrics_check)
        evidence["http_requests"] = 4 + len(evidence["attempts"])
        evidence["complete"] = bool(
            evidence.get("monitoring_completed")
            and evidence.get("kafka_monitoring_completed")
            and not evidence.get("resource_error")
            and not evidence.get("kafka_error")
        )
    finally:
        await observer.close()


async def run(run_dir):
    run_id = str(uuid4())
    evidence = {
        "run_id": run_id,
        "complete": False,
        "plan": [],
        "attempts": [],
        "peers": [],
        "samples": [],
        "kafka": [],
    }
    for index, (scheduled, rate) in enumerate(schedule()):
        evidence["plan"].append(
            {
                "client_message_id": str(uuid4()),
                "conversation_id": ROOM,
                "sender_id": ACTOR,
                "scheduled_seconds": scheduled,
                "stage_rps": rate,
                "text_sha256": sha256(
                    f"fanout-synthetic:{run_id}:{index}".encode()
                ).hexdigest(),
            }
        )
    write_new(
        run_dir / "manifest.json",
        {
            "run_id": run_id,
            "plan": evidence["plan"],
            "limits": {
                "messages": 210,
                "run_seconds": 120,
                "http_requests": 1000,
                "ws_peers": 2,
                "scale_writes": 1,
                "max_generator_lag_seconds": 1,
                "planned_gap_extra_seconds": 1,
            },
        },
    )
    try:
        async with asyncio.timeout(120):
            await exercise(evidence, run_dir)
    except EvidenceError as error:
        evidence["complete"] = False
        evidence["error"] = str(error)
        (run_dir / "STOP").touch(exist_ok=True)
    except Exception:  # noqa: BLE001 — sanitize all credential-bearing transport failures.
        evidence["complete"] = False
        evidence["error"] = "execution_incomplete"
        (run_dir / "STOP").touch(exist_ok=True)
    for peer in evidence["peers"]:
        peer.pop("ready", None)
        peer.pop("closed", None)
    result = compare(evidence)
    write_new(run_dir / "evidence.json", evidence)
    write_new(run_dir / "result.json", result)
    write_new(
        run_dir / "final.json",
        {
            "run_id": run_id,
            "complete": evidence["complete"],
            "planned_count": 210,
            "attempts": evidence["attempts"],
            "stop_reason": evidence.get("error"),
        },
    )
    print(json.dumps(result))
    return 0 if result["status"] == "pass" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-fanout-scale", action="store_true")
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    if not args.allow_fanout_scale:
        parser.error("--allow-fanout-scale is required for one lab Fanout 1→2 change")
    output = args.run_dir.resolve()
    root = Path(__file__).resolve().parents[3] / ".artifacts/chat-distributed"
    if not output.is_relative_to(root) or output == root:
        parser.error("Use a fresh directory below .artifacts/chat-distributed")
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    return asyncio.run(run(output))


if __name__ == "__main__":
    raise SystemExit(main())
