"""Bounded local HTTP/DB lock diagnostic; no Kafka/WS completeness claim."""

import argparse
import asyncio
import hashlib
import json
import math
import re
import shutil
import socket
import subprocess
import time
from collections import Counter
from pathlib import Path
from uuid import uuid4

import httpx2 as httpx

ROOT = Path(__file__).resolve().parents[3]
KUBE = [
    "kubectl",
    "--context",
    "k3d-laughtale-local",
    "--request-timeout=5s",
    "-n",
    "laughtale-chat-external",
]
DOCKER = ["docker", "--context", "desktop-linux"]
PRIMARY = "laughtale-postgres-lab-primary-1"
REPLICA = "laughtale-postgres-lab-replica-1"
NODE = "k3d-laughtale-local-server-0"
ROOM = "00000000-0000-4000-8000-000000000010"
ACTOR = "00000000-0000-4000-8000-000000000001"
BODY = "x" * 1024
HASH = hashlib.sha256(BODY.encode()).hexdigest()
SQL = """
SELECT coalesce(json_agg(row_to_json(s)), '[]') FROM (
 SELECT pid,application_name,state,wait_event_type,wait_event,
 pg_blocking_pids(pid) AS blockers,
 extract(epoch FROM clock_timestamp()-xact_start) AS transaction_age,
 CASE WHEN query ILIKE '%pg_advisory_xact_lock%' THEN 'advisory_lock'
 WHEN query ILIKE '%FOR UPDATE%' THEN 'row_lock'
 WHEN query ILIKE 'COMMIT%' THEN 'commit'
 WHEN query ILIKE '%count(%' THEN 'count'
 WHEN query ILIKE 'INSERT%' THEN 'insert'
 WHEN query ILIKE 'UPDATE%' THEN 'update' ELSE 'other' END AS operation
 FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()
 AND (state<>'idle' OR wait_event_type='Lock')
) s;
"""


def command(args, **kw):
    return subprocess.run(
        args, check=True, capture_output=True, text=True, timeout=8, **kw
    ).stdout


def sql(query, target=PRIMARY):
    return json.loads(
        command(
            DOCKER
            + [
                "exec",
                "-i",
                target,
                "psql",
                "-U",
                "postgres",
                "-d",
                "laughtale_chat",
                "-XAtq",
                "-v",
                "ON_ERROR_STOP=1",
            ],
            input="SET statement_timeout='2s';\n" + query,
        )
    )


def pods():
    items = json.loads(
        command(
            KUBE
            + [
                "get",
                "pods",
                "-l",
                "app in (chat,chat-relay,chat-gateway,chat-fanout)",
                "-o",
                "json",
            ]
        )
    )["items"]
    if not items:
        raise ValueError("no_pods")
    result = {}
    for p in items:
        statuses = p["status"].get("containerStatuses", [])
        if (
            p["status"].get("phase") != "Running"
            or not statuses
            or not all(s["ready"] for s in statuses)
        ):
            raise ValueError("pod_unready")
        result[p["metadata"]["uid"]] = [
            p["metadata"]["name"],
            sum(s["restartCount"] for s in statuses),
        ]
    return result


def guard(baseline):
    state = json.loads(command(DOCKER + ["inspect", NODE]))[0]
    if not state["State"]["Running"] or state["State"]["OOMKilled"]:
        raise ValueError("node_stopped_or_oom")
    free = int(
        re.search(r"free percentage: (\d+)%", command(["memory_pressure", "-Q"]))[1]
    )
    stats = json.loads(
        command(DOCKER + ["stats", "--no-stream", "--format", "{{json .}}", NODE])
    )
    percent = float(stats["MemPerc"].rstrip("%"))
    disk = shutil.disk_usage(ROOT).free / 1024**3
    if free < 15 or percent > 85 or disk < 15 or pods() != baseline:
        raise ValueError("resource_floor_or_pod_change")
    return {
        "at": time.time(),
        "host_free_percent": free,
        "node_memory_percent": percent,
        "internal_free_gib": disk,
    }


def append(path, value):
    with path.open("a") as f:
        f.write(json.dumps(value) + "\n")


def cpu_stat(target):
    raw = command(
        KUBE + ["exec", "pod/" + target, "--", "cat", "/sys/fs/cgroup/cpu.stat"]
    )
    return {
        key: int(value) for key, value in (line.split() for line in raw.splitlines())
    }


def validate_options(rate, seconds, rooms):
    if rate not in (10, 25, 50, 100) or not 1 <= seconds <= 100 or rooms not in (1, 10):
        raise ValueError("bounded_profile_required")
    if rate * seconds > 6000:
        raise ValueError("attempt_budget")


async def run(
    rate=10,
    seconds=100,
    rooms=1,
    *,
    before_send=None,
    http_keepalive=True,
    http_keepalive_expiry=5,
):
    validate_options(rate, seconds, rooms)
    count = rate * seconds
    folder = ROOT / ".artifacts/chat-lock" / str(uuid4())
    folder.mkdir(parents=True, exist_ok=False)
    result = {
        "status": "incomplete",
        "run_dir": str(folder),
        "planned": count,
        "rate": rate,
        "rooms": rooms,
        "seconds": seconds,
        "ws_kafka_verified": False,
        "http_keepalive": http_keepalive,
        "http_keepalive_expiry": http_keepalive_expiry,
    }
    stop = asyncio.Event()
    tasks = []
    forward = None
    forward_log = None
    rows = []
    ids = [str(uuid4()) for _ in range(count)]
    room_ids = [str(uuid4()) for _ in range(rooms)]
    room_for = {cid: room_ids[i % rooms] for i, cid in enumerate(ids)}
    (folder / "plan.json").write_text(
        json.dumps({"ids": ids, "rooms": room_for, "body_sha256": HASH})
    )
    try:
        baseline = await asyncio.to_thread(pods)
        append(folder / "resources.jsonl", await asyncio.to_thread(guard, baseline))
        pending_outbox = await asyncio.to_thread(
            sql,
            "SELECT to_json(count(*)) FROM chat.message_outbox WHERE published_at IS NULL;",
        )
        result["preflight_pending_outbox"] = pending_outbox
        if pending_outbox >= 100:
            raise ValueError("outbox_not_drained")
        fixtures = "BEGIN;"
        for room in room_ids:
            fixtures += f"INSERT INTO chat.conversations(id,kind,last_seq) VALUES ('{room}','dm',0);"
            fixtures += f"INSERT INTO chat.members(conversation_id,user_id) VALUES ('{room}','{ACTOR}'),('{room}','00000000-0000-4000-8000-000000000002');"
        fixtures += "COMMIT; SELECT to_json(true);"
        await asyncio.to_thread(sql, fixtures)
        result["baseline_pods"] = baseline
        target = min(
            p[0]
            for p in baseline.values()
            if p[0].startswith("chat-")
            and not p[0].startswith(("chat-relay", "chat-fanout", "chat-gateway"))
        )
        with socket.socket() as check:
            check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            check.bind(("127.0.0.1", 18092))
        forward_log = (folder / "api-forward.log").open("w")
        forward = await asyncio.to_thread(
            subprocess.Popen,
            KUBE
            + [
                "port-forward",
                "pod/" + target,
                "18092:18082",
                "--address",
                "127.0.0.1",
            ],
            stdout=forward_log,
            stderr=forward_log,
        )
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:18092",
            trust_env=False,
            timeout=3,
            headers={"Host": "127.0.0.1:18082", "Origin": "http://127.0.0.1:18083"},
            limits=httpx.Limits(
                max_connections=32,
                max_keepalive_connections=20 if http_keepalive else 0,
                keepalive_expiry=http_keepalive_expiry,
            ),
        ) as client:
            for attempt in range(20):
                try:
                    response = await client.get("/health/ready")
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
            else:
                raise ValueError("api_not_ready")
            response = await client.post("/v1/dev/session", json={"user": "user_a"})
            if (
                response.status_code != 200
                or response.json()["data"]["user_id"] != ACTOR
            ):
                raise ValueError("session_failed")

            if before_send is not None:
                await before_send(client, room_ids, folder)

            async def observe():
                while not stop.is_set():
                    try:
                        begin = time.monotonic()
                        data = await asyncio.to_thread(sql, SQL)
                        append(
                            folder / "waits.jsonl",
                            {
                                "at": time.time(),
                                "sample_seconds": time.monotonic() - begin,
                                "sessions": data,
                            },
                        )
                    except Exception as error:  # noqa: BLE001 - fail closed, no SQL or credentials
                        result["stop_reason"] = "db_observation_failed"
                        result["observer_error_type"] = type(error).__name__
                        stop.set()
                    await asyncio.sleep(0.2)

            async def monitor():
                while not stop.is_set():
                    try:
                        append(
                            folder / "resources.jsonl",
                            await asyncio.to_thread(guard, baseline),
                        )
                        if (folder / "STOP").exists():
                            raise ValueError("operator_stop")
                    except Exception as error:  # noqa: BLE001 - stop without logging sensitive exception text
                        result["stop_reason"] = "resource_guard"
                        result["guard_error_type"] = type(error).__name__
                        if isinstance(error, subprocess.TimeoutExpired):
                            result["guard_command"] = error.cmd[0]
                        if isinstance(error, ValueError) and str(error) in {
                            "node_stopped_or_oom",
                            "resource_floor_or_pod_change",
                            "operator_stop",
                            "pod_unready",
                            "no_pods",
                        }:
                            result["guard_detail"] = str(error)
                        stop.set()
                    await asyncio.sleep(3)

            async def send(cid, due):
                started = time.monotonic()
                row = {
                    "cid": cid,
                    "started": time.time(),
                    "started_monotonic": started,
                    "dispatch_delay": started - due,
                    "status": "unknown",
                }
                try:
                    r = await client.post(
                        f"/v1/internal-conversations/{room_for[cid]}/messages",
                        json={"client_message_id": cid, "text": BODY},
                    )
                    row["http_status"] = r.status_code
                    if r.status_code == 201:
                        m = r.json()["data"]
                        if (
                            m["client_message_id"] != cid
                            or m["conversation_id"] != room_for[cid]
                            or m["sender_id"] != ACTOR
                            or hashlib.sha256(m["text"].encode()).hexdigest() != HASH
                        ):
                            raise ValueError("ack_mismatch")
                        row.update(status="ack", mid=m["message_id"], seq=int(m["seq"]))
                    elif (
                        r.status_code == 503
                        and r.headers.get("X-Lab-Admission") == "rejected"
                    ):
                        row.update(status="rejected", error_code="admission_rejected")
                    else:
                        code = r.json().get("code")
                        row["error_code"] = (
                            code
                            if code in {"DATABASE_BUSY", "DATABASE_POOL_TIMEOUT"}
                            else "other"
                        )
                except (httpx.HTTPError, ValueError, KeyError) as error:
                    row["error_code"] = "transport_or_contract"
                    row["error_type"] = type(error).__name__
                row["seconds"] = time.monotonic() - started
                rows.append(row)
                append(folder / "requests.jsonl", row)

            tasks = [asyncio.create_task(observe()), asyncio.create_task(monitor())]
            pending = set()

            async def metrics_snapshot(name):
                response = await client.get("/metrics")
                if response.status_code != 200:
                    raise ValueError("metrics_unavailable")
                lines = [
                    line
                    for line in response.text.splitlines()
                    if line.startswith(
                        ("db_auth_phase_", "db_connection_acquire_", "db_pool_")
                    )
                ]
                (folder / name).write_text("\n".join(lines) + "\n")

            await metrics_snapshot("metrics-before.prom")
            result["target_pod"] = target
            pod_info = json.loads(command(KUBE + ["get", "pod", target, "-o", "json"]))
            container = pod_info["spec"]["containers"][0]
            result["runtime"] = {
                "image": container["image"],
                "resources": container.get("resources", {}),
                "admission_limit": next(
                    (
                        v.get("value")
                        for v in container.get("env", [])
                        if v["name"] == "LAB_MESSAGE_ADMISSION_LIMIT"
                    ),
                    "0",
                ),
            }
            result["cpu_before"] = await asyncio.to_thread(cpu_stat, target)
            start = time.monotonic()
            print("Running " + str(folder), flush=True)
            for i, cid in enumerate(ids):
                due = start + i / rate
                await asyncio.sleep(max(0, due - time.monotonic()))
                if stop.is_set():
                    break
                if len(pending) >= 32 or time.monotonic() - due > 0.1:
                    rows.append(
                        {
                            "cid": cid,
                            "status": "skipped",
                            "reason": "inflight_limit"
                            if len(pending) >= 32
                            else "dispatch_lag",
                            "dispatch_delay": time.monotonic() - due,
                        }
                    )
                    append(folder / "requests.jsonl", rows[-1])
                    continue
                task = asyncio.create_task(send(cid, due))
                pending.add(task)
                task.add_done_callback(pending.discard)
            await asyncio.gather(*pending)
            result["send_seconds"] = time.monotonic() - start
            await metrics_snapshot("metrics-after.prom")
            result["cpu_after"] = await asyncio.to_thread(cpu_stat, target)
            result["cpu_delta"] = {
                key: value - result["cpu_before"][key]
                for key, value in result["cpu_after"].items()
            }
            result["states"] = dict(Counter(r["status"] for r in rows))
            result["http_statuses"] = dict(
                Counter(str(r.get("http_status")) for r in rows)
            )
            latency = sorted(r["seconds"] for r in rows if r["status"] == "ack")
            result["ack_p99"] = (
                latency[math.ceil(len(latency) * 0.99) - 1] if latency else None
            )
            checks = {}
            for label, db in (("primary", PRIMARY), ("replica", REPLICA)):
                found = {}
                for offset in range(0, len(ids), 250):
                    values = ",".join(
                        "'" + cid + "'::uuid" for cid in ids[offset : offset + 250]
                    )
                    query = (
                        "SELECT coalesce(json_agg(row_to_json(s)),'[]') FROM (SELECT client_message_id::text cid,id::text mid,conversation_id::text room,seq,encode(sha256(convert_to(text,'UTF8')),'hex') hash FROM chat.messages WHERE client_message_id IN ("
                        + values
                        + ")) s;"
                    )
                    for row in await asyncio.to_thread(sql, query, db):
                        found[row["cid"]] = row
                (folder / (label + ".json")).write_text(json.dumps(found))
                checks[label] = (
                    all(
                        r["cid"] in found
                        and found[r["cid"]]["mid"] == r["mid"]
                        and found[r["cid"]]["seq"] == r["seq"]
                        and found[r["cid"]]["hash"] == HASH
                        and found[r["cid"]]["room"] == room_for[r["cid"]]
                        for r in rows
                        if r["status"] == "ack"
                    )
                    and bool(latency)
                    and not any(
                        r["cid"] in found for r in rows if r["status"] == "rejected"
                    )
                )
                result[label + "_stored"] = len(found)
                result[label + "_unknown_stored"] = sum(
                    r["cid"] in found for r in rows if r["status"] == "unknown"
                )
            append(folder / "resources.jsonl", await asyncio.to_thread(guard, baseline))
            result["checks"] = checks
            result["status"] = (
                "pass"
                if not stop.is_set()
                and len(latency) == count
                and result["ack_p99"] <= 1
                and all(checks.values())
                else "fail"
            )
            if stop.is_set():
                result["status"] = "incomplete"
    except Exception as error:  # noqa: BLE001 - retain incomplete result, no secrets
        result["error_type"] = type(error).__name__
        if isinstance(error, ValueError) and str(error) in {
            "outbox_not_drained",
            "resource_floor_or_pod_change",
            "pod_unready",
            "no_pods",
            "node_stopped_or_oom",
            "api_not_ready",
            "session_failed",
        }:
            result["error_code"] = str(error)
        if isinstance(error, OSError):
            result["os_errno"] = error.errno
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        if forward is not None:
            result["forward_exit_before_cleanup"] = forward.poll()
            forward.terminate()
            await asyncio.to_thread(forward.wait, timeout=8)
        if forward_log is not None:
            forward_log.close()
        result["attempt_records"] = len(rows)
        (folder / "result.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rate", type=int, default=10)
    parser.add_argument("--seconds", type=int, default=100)
    parser.add_argument("--rooms", type=int, default=1)
    args = parser.parse_args()
    outcome = asyncio.run(run(args.rate, args.seconds, args.rooms))["status"]
    raise SystemExit({"pass": 0, "fail": 1}.get(outcome, 2))
