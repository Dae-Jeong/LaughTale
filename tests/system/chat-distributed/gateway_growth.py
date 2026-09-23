"""E2: existing WS stays connected while a new cohort joins a new Gateway Pod.

Uses an Orca-owned direct port-forward for the new cohort, not automatic WS LB.
"""

import argparse
import asyncio
import json
import socket
import time
import urllib.request
from collections import Counter
from contextlib import AsyncExitStack
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from uuid import UUID, uuid4

from fanout_oracle import role_transition
from growth import KUBE, entry_request, kube_command, now, resource_snapshot
from growth_oracle import timestamp
from oracle import ACTOR, FIELDS, ROOM, EvidenceError, normalize
from runner import HOST, MESSAGE_PATH, LabHTTP, append, open_peer, write_new

INITIAL = {
    "chat": 2,
    "chat-gateway": 1,
    "chat-fanout": 2,
    "chat-relay": 1,
    "chat-growth-proxy": 1,
}


def schedule():
    return (
        [float(i) for i in range(10)]
        + [10 + i / 5 for i in range(50)]
        + [20 + i / 10 for i in range(150)]
    )


async def state():
    raw = json.loads(await kube_command("get", "deployment/chat-gateway", "-o", "json"))
    return {
        "uid": raw["metadata"]["uid"],
        "resource_version": raw["metadata"]["resourceVersion"],
        "replicas": raw["spec"]["replicas"],
        "ready": raw["status"].get("readyReplicas", 0),
    }


def health(port):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/health/ready", headers={"Host": HOST}
    )
    with opener.open(request, timeout=1) as response:
        data = json.loads(response.read(4096))
        return str(UUID(data["instance_id"]))


async def scale_and_join(e, directory, start, stack, cookie):
    await asyncio.sleep(max(0, start + 10 - time.monotonic()))
    before = await state()
    if (
        before["uid"] != e["initial_deployment"]["uid"]
        or before["replicas"] != 1
        or before["ready"] != 1
    ):
        raise EvidenceError("scale_precondition_failed")
    if (directory / "STOP").exists():
        raise EvidenceError("stopped_before_scale")
    e["scale"] = {
        "before": before,
        "requested_at": now(),
        "success": False,
        "writes": 1,
    }
    write_new(directory / "scale-request.json", e["scale"])
    await kube_command(
        "scale",
        "deployment/chat-gateway",
        "--current-replicas=1",
        "--replicas=2",
        "--resource-version=" + before["resource_version"],
    )
    e["scale"].update(success=True, completed_at=now())
    write_new(directory / "scale-result.json", e["scale"])
    old = {row["uid"] for row in e["samples"][0]["pods"]}
    for _ in range(30):
        if (directory / "STOP").exists():
            raise EvidenceError("stopped_during_join")
        rows = json.loads(
            await kube_command("get", "pods", "-l", "app=chat-gateway", "-o", "json")
        )["items"]
        new = [row for row in rows if row["metadata"]["uid"] not in old]
        if len(new) > 1:
            raise EvidenceError("unexpected_new_gateway")
        if new and any(
            c["type"] == "Ready" and c["status"] == "True"
            for c in new[0]["status"].get("conditions", [])
        ):
            pod = new[0]
            break
        await asyncio.sleep(0.5)
    else:
        raise EvidenceError("new_gateway_not_ready")
    e["new_gateway"] = {
        "uid": pod["metadata"]["uid"],
        "name": pod["metadata"]["name"],
        "ready_observed_at": now(),
    }
    command = " ".join(
        (
            *KUBE,
            "port-forward",
            "--address",
            "127.0.0.1",
            "pod/" + pod["metadata"]["name"],
            "18095:18082",
        )
    )
    process = await asyncio.create_subprocess_exec(
        "orca",
        "terminal",
        "create",
        "--worktree",
        "active",
        "--title",
        "chat-growth-new-gateway",
        "--command",
        command,
        "--json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 5)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    result = json.loads(output)
    if process.returncode or not result.get("ok"):
        raise EvidenceError("orca_forward_failed_no_retry")
    e["new_gateway"]["terminal_handle"] = result["result"]["terminal"]["handle"]
    for _ in range(20):
        try:
            instance = await asyncio.to_thread(health, 18095)
            break
        except (OSError, ValueError, KeyError):
            await asyncio.sleep(0.2)
    else:
        raise EvidenceError("forward_not_ready")
    _, peer = await open_peer(stack, "gateway_b", cookie, "new", e, directory)
    peer.update(pod_uid=pod["metadata"]["uid"], instance_id=instance, joined_at=now())


async def monitor(e, directory, done):
    baseline = e["samples"][0]
    while True:
        try:
            await asyncio.wait_for(done.wait(), 5)
        except TimeoutError:
            pass
        try:
            sample = await asyncio.to_thread(resource_snapshot)
            issues = role_transition(
                baseline, sample, "chat-gateway", bool(e.get("scale"))
            )
            sample["transition_issues"] = issues
            e["samples"].append(sample)
            append(directory / "resources.jsonl", sample)
            if issues:
                raise EvidenceError("resource_or_identity_failed")
        except Exception:  # noqa: BLE001 -- Fail closed without credential-bearing CLI errors.
            e["resource_error"] = True
            (directory / "STOP").touch()
            return
        if done.is_set():
            e["monitoring_completed"] = True
            return


def compare(e):
    issues = []
    samples, attempts, peers = e["samples"], e["attempts"], e["peers"]
    scale = e.get("scale", {})
    before = scale.get("before", {})
    if (
        before.get("replicas") != 1
        or before.get("ready") != 1
        or not before.get("resource_version")
    ):
        issues.append("scale_precondition_invalid")
    try:
        if (
            not timestamp(e["traffic_started_at"])
            <= timestamp(scale["requested_at"])
            <= timestamp(scale["completed_at"])
            <= timestamp(e["new_gateway"]["ready_observed_at"])
        ):
            issues.append("scale_time_order_invalid")
    except (KeyError, ValueError, TypeError):
        issues.append("scale_time_evidence_missing")
    if (
        not e.get("complete")
        or e.get("resource_error")
        or not e.get("monitoring_completed")
    ):
        issues.append("incomplete")
    if (
        scale.get("success") is not True
        or scale.get("writes") != 1
        or scale.get("before", {}).get("uid")
        != e.get("initial_deployment", {}).get("uid")
    ):
        issues.append("scale_evidence_invalid")
    if not samples or Counter(row["app"] for row in samples[0]["pods"]) != INITIAL:
        issues.append("initial_topology_invalid")
    if samples:
        added = set()
        for sample in samples:
            issues += role_transition(
                samples[0],
                sample,
                "chat-gateway",
                bool(scale.get("requested_at"))
                and timestamp(sample["observed_at"])
                >= timestamp(scale["requested_at"]),
            )
            added |= {p["uid"] for p in sample["pods"]} - {
                p["uid"] for p in samples[0]["pods"]
            }
        if added != {e.get("new_gateway", {}).get("uid")}:
            issues.append("new_gateway_identity_invalid")
        if (
            sum(p["app"] == "chat-gateway" and p["ready"] for p in samples[-1]["pods"])
            != 2
        ):
            issues.append("final_gateways_not_ready")
    if len(attempts) != 210 or any(
        row["outcome"] != "acknowledged" for row in attempts
    ):
        issues.append("plan_incomplete")
    planned = {row["client_message_id"]: row for row in e["plan"]}
    if len(planned) != 210 or len({a["client_message_id"] for a in attempts}) != len(
        attempts
    ):
        issues.append("invalid_intent_plan")
    ack = {}
    for row in attempts:
        if row.get("ack_status") not in (200, 201) or row.get("http_status") not in (
            200,
            201,
        ):
            issues.append("ack_http_status_invalid")
        message = row.get("message")
        if not message:
            issues.append("ack_payload_missing")
            continue
        if message["message_id"] in ack or any(
            message[k] != planned[row["client_message_id"]][k]
            for k in (
                "client_message_id",
                "conversation_id",
                "sender_id",
                "text_sha256",
            )
        ):
            issues.append("ack_mismatch_or_duplicate")
        ack[message["message_id"]] = message
    if len(ack) != 210:
        issues.append("ack_count_incomplete")
    if len({m["seq"] for m in ack.values()}) != len(ack):
        issues.append("duplicate_seq")
    if (
        len(peers) != 2
        or {p["target"] for p in peers} != {"gateway_a", "gateway_b"}
        or len({p.get("instance_id") for p in peers}) != 2
    ):
        issues.append("distinct_gateway_cohorts_required")
    counts = {}
    for peer in peers:
        try:
            if str(UUID(peer["instance_id"])) != peer["instance_id"]:
                raise ValueError("noncanonical_instance")
        except (KeyError, ValueError, TypeError, AttributeError):
            issues.append("gateway_instance_id_invalid")
        expected_uid = (
            e.get("new_gateway", {}).get("uid")
            if peer["id"] == "new"
            else next(
                (p["uid"] for p in samples[0]["pods"] if p["app"] == "chat-gateway"),
                None,
            )
            if samples
            else None
        )
        if peer.get("pod_uid") != expected_uid or not expected_uid:
            issues.append("peer_pod_identity_mismatch")
        expected = {
            key
            for key, row in ack.items()
            if int(row["seq"]) > int(peer["baseline_head"])
        }
        seen = set()
        if peer["id"] == "existing" and expected != set(ack):
            issues.append("existing_cohort_must_receive_all")
        if peer["id"] == "new":
            try:
                joined = timestamp(peer["joined_at"])
                if joined < timestamp(e["new_gateway"]["ready_observed_at"]):
                    issues.append("join_before_ready")
                future = {
                    a["message"]["message_id"]
                    for a in attempts
                    if a.get("message") and timestamp(a["started_at"]) >= joined
                }
                if not future or not future <= expected:
                    issues.append("new_cohort_hides_post_join_messages")
            except (KeyError, ValueError, TypeError):
                issues.append("new_cohort_join_time_missing")
        for row in peer["received"]:
            key = row["message_id"]
            if (
                key not in ack
                or any(row[k] != ack[key][k] for k in FIELDS)
                or key in seen
            ):
                issues.append("ws_mismatch_duplicate_or_unmatched")
            seen.add(key)
        if (
            not expected
            or not expected <= seen
            or peer.get("error")
            or not peer.get("alive_after_traffic")
        ):
            issues.append("cohort_delivery_failed")
        counts[peer["id"]] = {
            "expected_after_subscription": len(expected),
            "received": len(seen),
        }
    for left, right in pairwise(attempts):
        if (
            timestamp(right["started_at"]) - timestamp(left["started_at"])
            > right["planned_at_seconds"] - left["planned_at_seconds"] + 1
        ):
            issues.append("traffic_gap_exceeded")
    if any(row["generator_lag_seconds"] > 1 for row in attempts):
        issues.append("generator_lag_exceeded")
    return {
        "status": "fail" if issues else "pass",
        "issues": dict(Counter(issues)),
        "acknowledged": len(ack),
        "cohorts": counts,
        "scope": "E2 explicit new-Pod connection cohort; automatic WS load balancing unverified",
    }


async def exercise(e, directory):
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 18095)) == 0:
            raise EvidenceError("new_gateway_port_occupied")
    e["initial_deployment"] = await state()
    if (
        e["initial_deployment"]["replicas"] != 1
        or e["initial_deployment"]["ready"] != 1
    ):
        raise EvidenceError("one_gateway_required")
    sample = await asyncio.to_thread(resource_snapshot)
    if Counter(p["app"] for p in sample["pods"]) != INITIAL or role_transition(
        sample, sample, "chat-gateway", False
    ):
        raise EvidenceError("baseline_invalid")
    e["samples"].append(sample)
    append(directory / "resources.jsonl", sample)
    http = LabHTTP()
    status, _, cookie = await http.request(
        "api_a", "/v1/dev/session", method="POST", body={"user": "user_a"}
    )
    if status != 200 or not cookie:
        raise EvidenceError("session_failed")
    done = asyncio.Event()
    monitoring = asyncio.create_task(monitor(e, directory, done))
    joining = None
    try:
        async with AsyncExitStack() as stack:
            _, peer = await open_peer(
                stack, "gateway_a", cookie, "existing", e, directory
            )
            peer["instance_id"] = await asyncio.to_thread(health, 18094)
            peer["pod_uid"] = next(
                p["uid"] for p in sample["pods"] if p["app"] == "chat-gateway"
            )
            start = time.monotonic()
            e["traffic_started_at"] = now()
            joining = asyncio.create_task(
                scale_and_join(e, directory, start, stack, cookie)
            )
            for index, planned in enumerate(e["plan"]):
                await asyncio.sleep(
                    max(0, start + planned["planned_at_seconds"] - time.monotonic())
                )
                if (directory / "STOP").exists() or (
                    joining.done() and joining.exception()
                ):
                    raise EvidenceError("guard_or_join_stopped")
                sent = time.monotonic()
                row = {
                    **planned,
                    "attempt_id": index,
                    "outcome": "unknown",
                    "started_at": now(),
                    "generator_lag_seconds": max(
                        0, sent - start - planned["planned_at_seconds"]
                    ),
                }
                try:
                    code, data, uid = await asyncio.to_thread(
                        entry_request,
                        MESSAGE_PATH,
                        cookie,
                        {
                            "client_message_id": planned["client_message_id"],
                            "text": f"gateway-growth:{e['run_id']}:{index}",
                        },
                    )
                    row.update(http_status=code, pod_uid=uid)
                    if code in (200, 201):
                        message = normalize(data["data"])
                        row.update(
                            outcome="acknowledged",
                            ack_status=code,
                            message=message,
                            response_message_id=message["message_id"],
                            response_seq=message["seq"],
                        )
                    elif 400 <= code < 500:
                        row["outcome"] = "rejected"
                except (OSError, ValueError, KeyError):
                    row["reason"] = "transport_or_response_unknown"
                row.update(completed_at=now(), latency_seconds=time.monotonic() - sent)
                e["attempts"].append(row)
                append(directory / "requests.jsonl", row)
                if row["outcome"] != "acknowledged":
                    raise EvidenceError("request_not_acknowledged")
            await joining
            for _ in range(60):
                if all(
                    {
                        a["message"]["message_id"]
                        for a in e["attempts"]
                        if int(a["message"]["seq"]) > int(p["baseline_head"])
                    }
                    <= {m["message_id"] for m in p["received"]}
                    for p in e["peers"]
                ):
                    break
                await asyncio.sleep(0.1)
            for peer in e["peers"]:
                peer["alive_after_traffic"] = not peer["closed"].is_set()
    finally:
        done.set()
        if asyncio.current_task().cancelling():
            monitoring.cancel()
        if joining and not joining.done():
            joining.cancel()
        await asyncio.gather(
            monitoring, *([joining] if joining else []), return_exceptions=True
        )
    e["complete"] = not e.get("resource_error")


async def run(directory):
    e = {
        "run_id": str(uuid4()),
        "plan": [],
        "attempts": [],
        "peers": [],
        "samples": [],
        "complete": False,
    }
    for index, planned in enumerate(schedule()):
        e["plan"].append(
            {
                "client_message_id": str(uuid4()),
                "conversation_id": ROOM,
                "sender_id": ACTOR,
                "planned_at_seconds": planned,
                "text_sha256": sha256(
                    f"gateway-growth:{e['run_id']}:{index}".encode()
                ).hexdigest(),
            }
        )
    write_new(
        directory / "manifest.json",
        {
            "run_id": e["run_id"],
            "plan": e["plan"],
            "limits": {"messages": 210, "ws": 2, "seconds": 120},
        },
    )
    try:
        async with asyncio.timeout(120):
            await exercise(e, directory)
    except Exception as error:  # noqa: BLE001 -- Preserve incomplete evidence, never credential text.
        e["error_class"] = type(error).__name__
        e["error"] = (
            str(error) if isinstance(error, EvidenceError) else "execution_incomplete"
        )
        (directory / "STOP").touch()
    for peer in e["peers"]:
        peer.pop("ready", None)
        peer.pop("closed", None)
    result = compare(e)
    write_new(directory / "evidence.json", e)
    write_new(directory / "result.json", result)
    write_new(
        directory / "final.json",
        {
            "run_id": e["run_id"],
            "complete": result["status"] == "pass",
            "planned_count": 210,
            "attempts": e["attempts"],
            "stop_reason": e.get("error"),
        },
    )
    print(json.dumps(result))
    return int(result["status"] != "pass")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-gateway-scale", action="store_true")
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    root = Path(__file__).resolve().parents[3] / ".artifacts/chat-distributed"
    if (
        not args.allow_gateway_scale
        or directory == root
        or not directory.is_relative_to(root)
    ):
        parser.error("Explicit scale approval and fresh artifact child path required")
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    return asyncio.run(run(directory))


if __name__ == "__main__":
    raise SystemExit(main())
