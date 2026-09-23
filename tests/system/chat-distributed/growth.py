"""Opt-in E1 API 1→2 experiment. Only root executes this bounded lab runner."""

import argparse
import asyncio
import importlib.util
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections import Counter
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

from growth_oracle import compare_growth, transition_issues
from oracle import ACTOR, ROOM, EvidenceError, normalize
from runner import (
    HOST,
    MESSAGE_PATH,
    ORIGIN,
    LabHTTP,
    NoRedirect,
    append,
    open_peer,
    wait_receipts,
    write_new,
)

KUBE = ("kubectl", "--context", "k3d-laughtale-local", "-n", "laughtale-chat-external")
APPS = {
    "chat": 1,
    "chat-gateway": 2,
    "chat-fanout": 2,
    "chat-relay": 1,
    "chat-growth-proxy": 1,
}


def now():
    return datetime.now(timezone.utc).isoformat()


def load_resource_helpers():
    path = Path(__file__).resolve().parents[2] / "load/chat-internal/resources.py"
    spec = importlib.util.spec_from_file_location("growth_resource_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resource_snapshot():
    """Read-only sample with transition-aware Pod checks in a separate oracle."""
    helpers = load_resource_helpers()
    targets = helpers.CONTAINERS + ("laughtale-redis-lab-redis-1",)
    pressure = helpers.command("memory_pressure", "-Q")
    match = re.search(r"free percentage:\s*(\d+)%", pressure)
    if match is None:
        raise EvidenceError("host_memory_sample_missing")
    free = int(match.group(1))
    states = json.loads(helpers.command("docker", "inspect", *targets))
    identities = {}
    for row in states:
        if not row["State"]["Running"] or row["State"]["OOMKilled"]:
            raise EvidenceError("container_stopped_or_oom")
        identities[row["Name"].lstrip("/")] = [row["Id"], row["RestartCount"]]
    if set(identities) != set(targets):
        raise EvidenceError("container_identity_missing")
    stats = [
        json.loads(line)
        for line in helpers.command(
            "docker", "stats", "--no-stream", "--format", "{{json .}}", *targets
        ).splitlines()
    ]
    if {row["Name"] for row in stats} != set(targets):
        raise EvidenceError("container_metrics_missing")
    pod_data = json.loads(
        helpers.command(
            *KUBE,
            "get",
            "pods",
            "-l",
            "app in (chat,chat-gateway,chat-fanout,chat-relay,chat-growth-proxy)",
            "-o",
            "json",
        )
    )["items"]
    observed_at = now()
    pods = []
    for pod in pod_data:
        statuses = pod["status"].get("containerStatuses", [])
        condition = next(
            (
                row
                for row in pod["status"].get("conditions", [])
                if row["type"] == "Ready"
            ),
            {},
        )
        pods.append(
            {
                "uid": pod["metadata"]["uid"],
                "name": pod["metadata"]["name"],
                "app": pod["metadata"]["labels"]["app"],
                "ready": bool(statuses)
                and condition.get("status") == "True"
                and all(row["ready"] for row in statuses),
                "ready_since": condition.get("lastTransitionTime")
                if condition.get("status") == "True"
                else None,
                "restarts": sum(row["restartCount"] for row in statuses),
                "terminating": bool(pod["metadata"].get("deletionTimestamp")),
            }
        )
    outbox = json.loads(
        helpers.command(
            "docker",
            "exec",
            helpers.CONTAINERS[2],
            "psql",
            "-U",
            "postgres",
            "-d",
            "laughtale_chat",
            "-X",
            "-q",
            "-t",
            "-A",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            helpers.OUTBOX_SQL,
        ).strip()
    )
    disk = int(
        helpers.command(
            "docker", "exec", helpers.CONTAINERS[1], "du", "-sk", "/var/lib/kafka/data"
        ).split()[0]
    )
    reasons = []
    if free < 10:
        reasons.append("host_memory_low")
    for row in stats:
        threshold = 85 if row["Name"] == targets[0] else 95
        if float(row["MemPerc"].rstrip("%")) > threshold:
            reasons.append("container_memory_limit")
    if disk >= 3 * 1024 * 1024:
        reasons.append("kafka_disk_budget")
    return {
        "observed_at": observed_at,
        "completed_at": now(),
        "status": "stop" if reasons else "ok",
        "reasons": reasons,
        "containers": identities,
        "pods": pods,
        "host_free_percent": free,
        "kafka_volume_kib": disk,
        "outbox": outbox,
        "container_metrics": [
            {
                "name": row["Name"],
                "memory_percent": float(row["MemPerc"].rstrip("%")),
                "cpu_percent": float(row["CPUPerc"].rstrip("%")),
            }
            for row in stats
        ],
    }


async def kube_command(*args):
    process = await asyncio.create_subprocess_exec(
        *KUBE, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 5)
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()  # Only this exact child, never a workload or broad PID selection.
        await process.communicate()
        raise
    if process.returncode != 0:
        raise EvidenceError("kubectl_command_failed")
    return stdout.decode()


async def deployment():
    raw = json.loads(await kube_command("get", "deployment/chat", "-o", "json"))
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
        ):
            raise EvidenceError("scale_precondition_failed")
        if (run_dir / "STOP").exists():
            raise EvidenceError("stopped_before_scale")
        evidence["scale"] = {
            "requested_at": now(),
            "requested_monotonic": time.monotonic(),
            "writes": 1,
            "replicas_before": 1,
            "replicas_after": 2,
            "resource_version_before": before["resource_version"],
            "deployment_uid": before["uid"],
            "before": before,
            "success": False,
        }
        write_new(run_dir / "scale-request.json", evidence["scale"])
        await kube_command(
            "scale",
            "deployment/chat",
            "--replicas=2",
            "--current-replicas=1",
            "--resource-version=" + before["resource_version"],
        )
        evidence["scale"].update(
            success=True, completed_at=now(), completed_monotonic=time.monotonic()
        )
        write_new(run_dir / "scale-result.json", evidence["scale"])
        for _ in range(10):
            if (run_dir / "STOP").exists():
                raise EvidenceError("stopped_during_rollout")
            state = await deployment()
            if state["uid"] != before["uid"] or state["replicas"] != 2:
                raise EvidenceError("deployment_changed_outside_scale")
            if (
                state["ready"] == 2
                and state["observed_generation"] >= state["generation"]
            ):
                evidence["scale"]["rollout_ready_observed_at"] = now()
                return
            await asyncio.sleep(2)
        raise EvidenceError("rollout_not_ready")
    except (EvidenceError, TimeoutError, OSError, ValueError, KeyError):
        evidence.setdefault("scale", {})["error"] = (
            "scale_or_rollout_incomplete_no_retry"
        )
        (run_dir / "STOP").touch(exist_ok=True)


def entry_request(path, cookie, body=None):
    if path not in ("/v1/session", MESSAGE_PATH):
        raise EvidenceError("entry_path_not_allowed")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(
        "http://127.0.0.1:18096" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Host": "127.0.0.1:18096",
            "Origin": ORIGIN,
            "Cookie": "chat_session=" + cookie,
            "Content-Type": "application/json",
        },
        method="POST" if body is not None else "GET",
    )
    try:
        response = opener.open(request, timeout=3)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read(100001)
        if len(raw) > 100000:
            raise EvidenceError("entry_response_limit")
        uid = response.headers.get("X-Lab-Pod-UID")
        if response.status in (200, 201) and (not uid or str(UUID(uid)) != uid):
            raise EvidenceError("handling_pod_uid_missing")
        return response.status, json.loads(raw), uid


def metrics_check():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(
        "http://127.0.0.1:18092/metrics", headers={"Host": HOST}
    )
    with opener.open(request, timeout=3) as response:
        raw = response.read(1_000_001).decode()
        if response.status != 200 or len(raw) > 1_000_000:
            raise EvidenceError("metrics_preflight_failed")
        lines = [
            line
            for line in raw.splitlines()
            if line.startswith(("db_pool_", "db_sessions_", "db_transactions_"))
        ]
        if not lines:
            raise EvidenceError("required_db_metrics_missing")
        return lines


async def monitor(evidence, run_dir, done):
    baseline = evidence["samples"][0]
    observed_new = set()
    old = {row["uid"] for row in baseline["pods"]}
    while True:
        try:
            await asyncio.wait_for(done.wait(), 5)
        except TimeoutError:
            pass
        try:
            sample = await asyncio.to_thread(resource_snapshot)
            issues = transition_issues(
                baseline, sample, bool(evidence.get("scale", {}).get("requested_at"))
            )
            observed_new.update(
                row["uid"] for row in sample["pods"] if row["uid"] not in old
            )
            if len(observed_new) > 1:
                issues.append("more_than_one_new_uid_seen")
            sample["transition_issues"] = issues
            evidence["samples"].append(sample)
            append(run_dir / "resources.jsonl", sample)
            if issues:
                raise EvidenceError("transition_resource_failed")
        except (
            EvidenceError,
            subprocess.SubprocessError,
            OSError,
            KeyError,
            ValueError,
        ):
            evidence["resource_error"] = "resource_or_identity_check_failed"
            (run_dir / "STOP").touch(exist_ok=True)
            return
        if done.is_set():
            evidence["monitoring_completed"] = True
            return


async def exercise(evidence, run_dir):
    if (
        await kube_command(
            "auth", "can-i", "update", "deployments/chat", "--subresource=scale"
        )
    ).strip() != "yes":
        raise EvidenceError("scale_permission_denied")
    evidence["initial_deployment"] = await deployment()
    if (
        evidence["initial_deployment"]["replicas"] != 1
        or evidence["initial_deployment"]["ready"] != 1
    ):
        raise EvidenceError("initial_deployment_not_one_ready")
    baseline = await asyncio.to_thread(resource_snapshot)
    if Counter(row["app"] for row in baseline["pods"]) != APPS or transition_issues(
        baseline, baseline, False
    ):
        raise EvidenceError("initial_fixed_topology_invalid")
    evidence["samples"].append(baseline)
    append(run_dir / "resources.jsonl", baseline)
    evidence["initial_db_metrics"] = await asyncio.to_thread(metrics_check)
    direct = LabHTTP()
    status, data, cookie = await direct.request(
        "api_a", "/v1/dev/session", method="POST", body={"user": "user_a"}
    )
    if status != 200 or not cookie or data["data"]["user_id"] != ACTOR:
        raise EvidenceError("session_issue_failed")
    status, data, uid = await asyncio.to_thread(entry_request, "/v1/session", cookie)
    initial_uids = {row["uid"] for row in baseline["pods"] if row["app"] == "chat"}
    if status != 200 or data["data"]["user_id"] != ACTOR or uid not in initial_uids:
        raise EvidenceError("entry_session_or_pod_identity_failed")
    evidence["session_entry_verified"] = True
    done = asyncio.Event()
    monitor_task = asyncio.create_task(monitor(evidence, run_dir, done))
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
            for index, planned in enumerate(evidence["plan"]):
                await asyncio.sleep(max(0, start + index / 5 - time.monotonic()))
                if (run_dir / "STOP").exists():
                    raise EvidenceError("operator_or_guard_stop")
                sent = time.monotonic()
                row = {
                    **planned,
                    "attempt_id": index,
                    "outcome": "unknown",
                    "ack_status": None,
                    "started_at": now(),
                    "generator_lag_seconds": max(0, sent - start - index / 5),
                }
                try:
                    status, data, uid = await asyncio.to_thread(
                        entry_request,
                        MESSAGE_PATH,
                        cookie,
                        {
                            "client_message_id": planned["client_message_id"],
                            "text": f"growth-synthetic:{evidence['run_id']}:{index}",
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
                row.update(completed_at=now(), latency_seconds=time.monotonic() - sent)
                evidence["attempts"].append(row)
                append(run_dir / "requests.jsonl", row)
                if row["outcome"] != "acknowledged":
                    (run_dir / "STOP").touch(exist_ok=True)
                    raise EvidenceError("request_not_acknowledged")
            await wait_receipts(evidence)
            await scaling
            if (run_dir / "STOP").exists():
                raise EvidenceError("transition_stopped")
            for peer in evidence["peers"]:
                peer["alive_after_traffic"] = not peer["closed"].is_set()
    finally:
        done.set()
        if asyncio.current_task().cancelling():
            monitor_task.cancel()
        if scaling is not None and not scaling.done():
            scaling.cancel()
        await asyncio.gather(
            monitor_task, *([scaling] if scaling else []), return_exceptions=True
        )
    evidence["final_db_metrics"] = await asyncio.to_thread(metrics_check)
    evidence["http_requests"] = 4 + len(evidence["attempts"])
    evidence["complete"] = (
        not evidence.get("resource_error")
        and evidence.get("monitoring_completed") is True
    )


async def run(run_dir):
    run_id = str(uuid4())
    evidence = {
        "run_id": run_id,
        "complete": False,
        "plan": [],
        "attempts": [],
        "peers": [],
        "samples": [],
    }
    for index in range(200):
        evidence["plan"].append(
            {
                "client_message_id": str(uuid4()),
                "conversation_id": ROOM,
                "sender_id": ACTOR,
                "text_sha256": sha256(
                    f"growth-synthetic:{run_id}:{index}".encode()
                ).hexdigest(),
            }
        )
    write_new(
        run_dir / "manifest.json",
        {
            "run_id": run_id,
            "plan": evidence["plan"],
            "rate": 5,
            "duration_seconds": 40,
            "limits": {
                "messages": 200,
                "http_requests": 1000,
                "ws_peers": 2,
                "run_seconds": 120,
                "scale_writes": 1,
                "max_start_gap_seconds": 1.0,
                "max_generator_lag_seconds": 1.0,
            },
        },
    )
    try:
        async with asyncio.timeout(120):
            await exercise(evidence, run_dir)
    except EvidenceError as error:
        evidence["error"] = str(error)
        (run_dir / "STOP").touch(exist_ok=True)
    except Exception:  # noqa: BLE001 — never expose credentials in transport exceptions.
        evidence["error"] = "execution_incomplete"
        (run_dir / "STOP").touch(exist_ok=True)
    for peer in evidence["peers"]:
        peer.pop("ready", None)
        peer.pop("closed", None)
    result = compare_growth(evidence)
    write_new(run_dir / "evidence.json", evidence)
    write_new(run_dir / "result.json", result)
    write_new(
        run_dir / "final.json",
        {
            "run_id": run_id,
            "complete": evidence["complete"],
            "planned_count": 200,
            "attempts": evidence["attempts"],
            "stop_reason": evidence.get("error"),
        },
    )
    print(json.dumps(result))
    return 0 if result["status"] == "pass" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-api-scale",
        action="store_true",
        help="Allow exactly one preconditioned API 1→2 scale write",
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    if not args.allow_api_scale:
        parser.error(
            "--allow-api-scale is required; this experiment changes the lab API replica count"
        )
    output = args.run_dir.resolve()
    root = Path(__file__).resolve().parents[3] / ".artifacts/chat-distributed"
    if not output.is_relative_to(root) or output == root:
        parser.error("Use a fresh directory below .artifacts/chat-distributed")
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    return asyncio.run(run(output))


if __name__ == "__main__":
    raise SystemExit(main())
