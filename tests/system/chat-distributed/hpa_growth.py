"""Root-only bounded CPU HPA experiment. This runner NEVER changes Kubernetes state."""

import argparse
import asyncio
import json
import time
from collections import Counter
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from growth import APPS, entry_request, kube_command, now, resource_snapshot
from growth_oracle import transition_issues
from hpa_growth_oracle import COUNT, PHASES, compare, cpu_millicores, idle_issues
from oracle import ACTOR, ROOM, EvidenceError, normalize
from runner import MESSAGE_PATH, LabHTTP, append, connection, event_message, write_new

SAFE_ERRORS = frozenset(
    {
        "fixed_api_cpu_or_replica_budget_changed",
        "fixed_primary_pool_budget_changed",
        "deployment_replaced",
        "api_metrics_missing",
        "api_metrics_stale",
        "control_hpa_present",
        "one_hpa_required",
        "fixed_hpa_policy_changed",
        "hpa_replaced",
        "initial_fixed_topology_invalid",
        "initial_outbox_not_drained",
        "resource_or_pod_transition_invalid",
        "more_than_two_api_uids",
        "bounded_600_receipt_limit",
        "unexpected_subscription",
        "unexpected_ws_frame",
        "history_request_failed",
        "history_snapshot_changed",
        "history_600_limit",
        "history_sequence_gap",
        "history_cursor_stalled",
        "history_page_limit",
        "idle_baseline_failed_no_threshold_lowering",
        "session_issue_failed",
        "baseline_heads_disagree",
        "operator_or_guard_stop",
        "request_unknown_or_generator_lag",
        "ws_closed_before_receipts",
        "kubectl_command_failed",
        "host_memory_sample_missing",
        "container_stopped_or_oom",
        "container_identity_missing",
        "container_metrics_missing",
    }
)


def safe_error(error):
    return (
        str(error)
        if isinstance(error, EvidenceError) and str(error) in SAFE_ERRORS
        else "evidence_validation_failed"
    )


def final_ledger(evidence, result):
    return {
        "run_id": evidence["run_id"],
        "complete": evidence["complete"],
        "planned_count": COUNT,
        "attempts": evidence["attempts"],
        "stop_reason": evidence.get("error"),
        "hpa_status": result["status"],
        "hpa_triggered": result["hpa_triggered"],
    }


def traffic_plan(run_id):
    result, offset = [], 0.0
    for rate, duration in PHASES:
        for index in range(rate * duration):
            text = f"hpa-synthetic:{run_id}:{len(result)}"
            result.append(
                {
                    "client_message_id": str(uuid4()),
                    "conversation_id": ROOM,
                    "sender_id": ACTOR,
                    "text_sha256": sha256(text.encode()).hexdigest(),
                    "rate": rate,
                    "scheduled_seconds": offset + index / rate,
                }
            )
        offset += duration
    return result


async def observe(evidence):
    raw, metrics, hpas, events = await asyncio.gather(
        kube_command("get", "deployment/chat", "-o", "json"),
        kube_command(
            "get",
            "--raw",
            "/apis/metrics.k8s.io/v1beta1/namespaces/laughtale-chat-external/pods",
        ),
        kube_command("get", "hpa", "-o", "json"),
        kube_command("get", "events", "-o", "json"),
    )
    deploy = json.loads(raw)
    replicas = deploy["spec"]["replicas"]
    containers = deploy["spec"]["template"]["spec"]["containers"]
    if (
        len(containers) != 1
        or containers[0]["resources"]["requests"]["cpu"] != "100m"
        or replicas not in (1, 2)
    ):
        raise EvidenceError("fixed_api_cpu_or_replica_budget_changed")
    pool = {
        item["name"]: item.get("value")
        for item in containers[0].get("env", [])
        if item["name"] in {"DB_POOL_SIZE", "DB_POOL_MAX_OVERFLOW"}
    }
    if pool != {"DB_POOL_SIZE": "2", "DB_POOL_MAX_OVERFLOW": "0"}:
        raise EvidenceError("fixed_primary_pool_budget_changed")
    if (
        evidence.get("deployment_uid", deploy["metadata"]["uid"])
        != deploy["metadata"]["uid"]
    ):
        raise EvidenceError("deployment_replaced")
    evidence["deployment_uid"] = deploy["metadata"]["uid"]
    rows = [
        row
        for row in json.loads(metrics)["items"]
        if row["metadata"].get("labels", {}).get("app") == "chat"
    ]
    if not rows:
        raise EvidenceError("api_metrics_missing")
    for row in rows:
        stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        if not 0 <= (datetime.now(timezone.utc) - stamp).total_seconds() <= 45:
            raise EvidenceError("api_metrics_stale")
    sample = {
        "observed_at": now(),
        "elapsed": time.monotonic() - evidence["started"],
        "replicas": replicas,
        "ready": deploy["status"].get("readyReplicas", 0),
        "cpu_millicores": sum(
            cpu_millicores(container["usage"]["cpu"])
            for row in rows
            for container in row["containers"]
        )
        / len(rows),
        "metrics_timestamp": min(row["timestamp"] for row in rows),
        "metrics_pod_count": len(rows),
        "desired_replicas": 1,
    }
    selected = [
        row
        for row in json.loads(hpas)["items"]
        if row["spec"]["scaleTargetRef"].get("name") == "chat"
    ]
    if evidence["mode"] == "control":
        if selected:
            raise EvidenceError("control_hpa_present")
    else:
        if len(selected) != 1:
            raise EvidenceError("one_hpa_required")
        hpa = selected[0]
        spec = hpa["spec"]
        expected = [
            {
                "type": "Resource",
                "resource": {
                    "name": "cpu",
                    "target": {"type": "Utilization", "averageUtilization": 60},
                },
            }
        ]
        if (
            hpa["metadata"]["name"] != "chat-growth"
            or spec["minReplicas"] != 1
            or spec["maxReplicas"] != 2
            or spec["metrics"] != expected
        ):
            raise EvidenceError("fixed_hpa_policy_changed")
        uid = hpa["metadata"]["uid"]
        if evidence.get("hpa_uid", uid) != uid:
            raise EvidenceError("hpa_replaced")
        evidence["hpa_uid"] = uid
        evidence["hpa_present"] = True
        status = hpa.get("status", {})
        sample["desired_replicas"] = status.get("desiredReplicas", 1)
        sample["hpa_status"] = status
        utilization = next(
            (
                item["resource"]["current"].get("averageUtilization", 0)
                for item in status.get("currentMetrics", [])
                if item.get("resource", {}).get("name") == "cpu"
            ),
            0,
        )
        if evidence.get("traffic_started_at"):
            if sample["desired_replicas"] == 2 and utilization > 60:
                evidence["hpa"]["cpu_desired_two"] = True
            for event in json.loads(events)["items"]:
                stamp = event.get("lastTimestamp") or event.get("eventTime") or ""
                message = event.get("message", "")
                controller = event.get("reportingComponent") or event.get(
                    "source", {}
                ).get("component")
                if (
                    event.get("involvedObject", {}).get("uid") == uid
                    and event.get("reason") == "SuccessfulRescale"
                    and "New size: 2" in message
                    and "cpu" in message.lower()
                    and controller == "horizontal-pod-autoscaler"
                    and datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    >= datetime.fromisoformat(evidence["traffic_started_at"])
                ):
                    evidence["hpa"]["successful_rescale"] = True
                    sample["rescale_event"] = {
                        "uid": event["metadata"]["uid"],
                        "last_timestamp": stamp,
                        "reason": "SuccessfulRescale",
                        "message": message,
                    }
    return sample


async def sample_all(evidence, directory):
    observation, resource = await asyncio.gather(
        observe(evidence), asyncio.to_thread(resource_snapshot)
    )
    if not evidence["resources"]:
        if Counter(row["app"] for row in resource["pods"]) != APPS:
            raise EvidenceError("initial_fixed_topology_invalid")
        if resource["outbox"]["pending"] != 0:
            raise EvidenceError("initial_outbox_not_drained")
        evidence["initial_api_uids"] = [
            row["uid"] for row in resource["pods"] if row["app"] == "chat"
        ]
    issues = transition_issues(
        evidence["resources"][0] if evidence["resources"] else resource,
        resource,
        evidence["mode"] == "hpa" and bool(evidence.get("traffic_started_at")),
    )
    if issues:
        raise EvidenceError("resource_or_pod_transition_invalid")
    observed = set(evidence["observed_api_uids"])
    ready = set(evidence["ready_api_uids"])
    for row in resource["pods"]:
        if row["app"] == "chat":
            observed.add(row["uid"])
            if row["ready"]:
                ready.add(row["uid"])
    if len(observed) > 2:
        raise EvidenceError("more_than_two_api_uids")
    evidence["observed_api_uids"], evidence["ready_api_uids"] = (
        sorted(observed),
        sorted(ready),
    )
    evidence["resources"].append(resource)
    evidence["observations"].append(observation)
    append(directory / "resources.jsonl", resource)
    append(directory / "hpa.jsonl", observation)
    return observation


async def monitor(evidence, directory, done):
    try:
        while not done.is_set():
            await sample_all(evidence, directory)
            try:
                await asyncio.wait_for(done.wait(), 5)
            except TimeoutError:
                pass
    except EvidenceError as error:
        evidence["error"] = safe_error(error)
        (directory / "STOP").touch(exist_ok=True)
    except Exception:  # noqa: BLE001 — fail closed without dumping credentials.
        evidence["error"] = "resource_or_hpa_observation_failed"
        (directory / "STOP").touch(exist_ok=True)


async def receive(ws, peer, directory):
    try:
        async for raw in ws:
            frame = json.loads(raw)
            if frame.get("type") == "message.created":
                if len(peer["received"]) >= 600:
                    raise EvidenceError("bounded_600_receipt_limit")
                row = event_message(frame)
                peer["received"].append(row)
                append(directory / "receipts.jsonl", {"peer": peer["target"], **row})
            elif frame.get("type") == "subscribed":
                if (
                    frame.get("conversation_id") != ROOM
                    or frame.get("protocol_version") != 1
                ):
                    raise EvidenceError("unexpected_subscription")
                peer["baseline_head"] = frame["head_seq"]
                peer["ready"].set()
            elif frame.get("type") != "heads":
                raise EvidenceError("unexpected_ws_frame")
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — persist only a bounded transport error code.
        peer["error"] = "ws_closed_or_invalid"
    finally:
        peer["closed"].set()


async def history(direct, cookie, after):
    recovered, snapshot, cursor = [], None, str(after)
    for _ in range(6):
        suffix = f"?after_seq={cursor}&limit=100"
        if snapshot is not None:
            suffix += f"&snapshot_head_seq={snapshot}"
        status, data, _ = await direct.request(
            "api_a", MESSAGE_PATH + suffix, cookie=cookie
        )
        if status != 200:
            raise EvidenceError("history_request_failed")
        meta = data["meta"]
        if snapshot is not None and snapshot != meta["snapshot_head_seq"]:
            raise EvidenceError("history_snapshot_changed")
        snapshot = meta["snapshot_head_seq"]
        recovered.extend(normalize(row) for row in data["data"])
        if len(recovered) > 600:
            raise EvidenceError("history_600_limit")
        if not meta["has_more"]:
            if [int(row["seq"]) for row in recovered] != list(
                range(int(after) + 1, int(snapshot) + 1)
            ):
                raise EvidenceError("history_sequence_gap")
            return recovered
        next_cursor = meta["next_cursor"]
        if int(next_cursor) <= int(cursor):
            raise EvidenceError("history_cursor_stalled")
        cursor = next_cursor
    raise EvidenceError("history_page_limit")


async def exercise(evidence, directory):
    # Startup/idle samples are separate from request-induced HPA evidence.
    while time.monotonic() - evidence["started"] < 60:
        evidence["idle"].append(await sample_all(evidence, directory))
        await asyncio.sleep(5)
    evidence["idle"].append(await sample_all(evidence, directory))
    if idle_issues(evidence["idle"]):
        raise EvidenceError("idle_baseline_failed_no_threshold_lowering")
    direct = LabHTTP()
    status, data, cookie = await direct.request(
        "api_a", "/v1/dev/session", method="POST", body={"user": "user_a"}
    )
    if status != 200 or not cookie or data["data"]["user_id"] != ACTOR:
        raise EvidenceError("session_issue_failed")
    done = asyncio.Event()
    watcher = asyncio.create_task(monitor(evidence, directory, done))
    receivers = []
    try:
        async with AsyncExitStack() as stack:
            for target in ("gateway_a", "gateway_b"):
                ws = await stack.enter_async_context(connection(target, cookie))
                peer = {
                    "target": target,
                    "received": [],
                    "ready": asyncio.Event(),
                    "closed": asyncio.Event(),
                }
                evidence["peers"].append(peer)
                receivers.append(asyncio.create_task(receive(ws, peer, directory)))
                await ws.send(
                    json.dumps({"type": "subscribe", "conversation_id": ROOM})
                )
                await asyncio.wait_for(peer["ready"].wait(), 4)
            if len({peer["baseline_head"] for peer in evidence["peers"]}) != 1:
                raise EvidenceError("baseline_heads_disagree")
            start = time.monotonic()
            evidence["traffic_started_at"] = now()
            for index, planned in enumerate(evidence["plan"]):
                await asyncio.sleep(
                    max(0, start + planned["scheduled_seconds"] - time.monotonic())
                )
                if (directory / "STOP").exists():
                    raise EvidenceError("operator_or_guard_stop")
                sent = time.monotonic()
                attempt = planned | {
                    "attempt_id": index,
                    "outcome": "unknown",
                    "ack_status": None,
                    "response_message_id": None,
                    "response_seq": None,
                    "started_at": now(),
                    "generator_lag_seconds": max(
                        0, sent - start - planned["scheduled_seconds"]
                    ),
                }
                try:
                    status, data, uid = await asyncio.to_thread(
                        entry_request,
                        MESSAGE_PATH,
                        cookie,
                        {
                            "client_message_id": planned["client_message_id"],
                            "text": f"hpa-synthetic:{evidence['run_id']}:{index}",
                        },
                    )
                    attempt.update(http_status=status, pod_uid=uid)
                    if status in (200, 201):
                        message = normalize(data["data"])
                        attempt.update(
                            outcome="acknowledged",
                            message=message,
                            ack_status=status,
                            response_message_id=message["message_id"],
                            response_seq=message["seq"],
                        )
                except Exception:  # noqa: BLE001 — an unknown POST is never retried.
                    attempt["reason"] = "transport_or_response_unknown"
                attempt.update(
                    completed_at=now(), latency_seconds=time.monotonic() - sent
                )
                evidence["attempts"].append(attempt)
                append(directory / "requests.jsonl", attempt)
                if (
                    attempt["outcome"] != "acknowledged"
                    or attempt["generator_lag_seconds"] > 1
                ):
                    raise EvidenceError("request_unknown_or_generator_lag")
            async with asyncio.timeout(30):
                while any(len(peer["received"]) < COUNT for peer in evidence["peers"]):
                    if any(peer["closed"].is_set() for peer in evidence["peers"]):
                        raise EvidenceError("ws_closed_before_receipts")
                    await asyncio.sleep(0.2)
            evidence["history"] = await history(
                direct, cookie, evidence["peers"][0]["baseline_head"]
            )
            # Observe delayed controller decisions without adding message writes.
            while (
                evidence["mode"] == "hpa"
                and time.monotonic() - evidence["started"] < 270
            ):
                if (
                    evidence["hpa"].get("successful_rescale")
                    and len(evidence["ready_api_uids"]) == 2
                ):
                    break
                if (directory / "STOP").exists():
                    raise EvidenceError("operator_or_guard_stop")
                await asyncio.sleep(2)
            for peer in evidence["peers"]:
                peer["alive_after_traffic"] = not peer["closed"].is_set()
            await sample_all(evidence, directory)
            evidence["complete"] = not evidence.get("error")
    finally:
        done.set()
        watcher.cancel()
        for task in receivers:
            task.cancel()
        await asyncio.gather(watcher, *receivers, return_exceptions=True)


async def run(directory, mode):
    evidence = {
        "run_id": str(uuid4()),
        "mode": mode,
        "started": time.monotonic(),
        "scale_writes": 0,
        "hpa_present": False,
        "hpa": {"target": 60},
        "complete": False,
        "idle": [],
        "observations": [],
        "resources": [],
        "attempts": [],
        "peers": [],
        "history": [],
        "initial_api_uids": [],
        "observed_api_uids": [],
        "ready_api_uids": [],
    }
    evidence["plan"] = traffic_plan(evidence["run_id"])
    write_new(directory / "plan.json", evidence["plan"])
    try:
        async with asyncio.timeout(300):
            await exercise(evidence, directory)
    except EvidenceError as error:
        evidence.setdefault("error", safe_error(error))
        (directory / "STOP").touch(exist_ok=True)
    except Exception:  # noqa: BLE001 — top-level evidence must survive any failure.
        evidence.setdefault("error", "bounded_experiment_incomplete")
        (directory / "STOP").touch(exist_ok=True)
    for peer in evidence["peers"]:
        peer.pop("ready", None)
        peer.pop("closed", None)
    result = compare(evidence)
    write_new(directory / "evidence.json", evidence)
    write_new(directory / "result.json", result)
    write_new(directory / "final.json", final_ledger(evidence, result))
    print(json.dumps(result), flush=True)
    return (
        0
        if result["status"] == "pass"
        else 2
        if result["status"] == "not_triggered"
        else 1
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("control", "hpa"), required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--allow-synthetic-load", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3] / ".artifacts/chat-distributed"
    output = args.run_dir.resolve()
    if (
        not args.allow_synthetic_load
        or not output.is_relative_to(root)
        or output == root
    ):
        parser.error(
            "Explicit synthetic-load opt-in and fresh .artifacts/chat-distributed child required"
        )
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    return asyncio.run(run(output, args.mode))


if __name__ == "__main__":
    raise SystemExit(main())
