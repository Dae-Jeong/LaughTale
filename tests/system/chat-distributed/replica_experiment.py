"""Root opt-in E4a: resume an owned pause with a separate 15s cleanup budget.

SIGINT/SIGTERM request cooperative cancellation and preserve recovery evidence.
SIGKILL, process/host loss or an unreachable database require external recovery;
this controller cannot promise successful resume under those conditions.
"""

import argparse
import asyncio
import json
import signal
import time
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import replica_history as history
from growth import entry_request, now, resource_snapshot
from oracle import ACTOR, FIELDS, ROOM, EvidenceError, normalize
from runner import MESSAGE_PATH, LabHTTP, append, write_new


async def admin(sql):
    process = await asyncio.create_subprocess_exec(
        "docker",
        "exec",
        "laughtale-postgres-lab-replica-1",
        "psql",
        "-U",
        "postgres",
        "-d",
        "laughtale_chat",
        "-X",
        "-At",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        sql,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(), 5)
    except BaseException:
        if process.returncode is None:
            process.kill()
        try:
            await asyncio.wait_for(process.communicate(), 1)
        except TimeoutError:
            pass  # The exact child was killed; never block recovery indefinitely.
        raise
    if process.returncode:
        raise EvidenceError("replica_admin_failed")
    return out.decode().strip()


async def resume_owned_pause(result):
    """A second cancellation must not cancel the separately owned recovery task."""
    started = time.monotonic()
    result["recovery_required"] = True

    async def restore():
        try:
            # Reserve two seconds of the 15s envelope for child reap/evidence.
            async with asyncio.timeout(13):
                for _ in range(3):
                    try:
                        await admin("SELECT pg_wal_replay_resume();")
                        if await admin("SELECT pg_is_wal_replay_paused();") == "f":
                            result.update(resumed=True, recovery_required=False)
                            return
                    except Exception:  # noqa: BLE001 — recovery errors contain no safe detail.
                        result["last_resume_attempt"] = "failed"
                    await asyncio.sleep(0.2)
        except TimeoutError:
            result["recovery_error"] = "resume_deadline_exceeded"
        if not result["resumed"]:
            result.setdefault("recovery_error", "resume_unconfirmed")

    task = asyncio.create_task(restore())
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    await task
    result["resumed_at"] = now()
    result["recovery_seconds"] = time.monotonic() - started
    if interrupted:
        raise asyncio.CancelledError()


def bind_reference(attempts, reference):
    if len(attempts) != 20 or len(reference) != 20:
        raise EvidenceError("intent_reference_count_mismatch")
    expected = []
    for attempt in attempts:
        if attempt["outcome"] != "acknowledged":
            raise EvidenceError("intent_not_acknowledged")
        message = attempt["message"]
        if any(
            message[field] != attempt[field]
            for field in (
                "client_message_id",
                "conversation_id",
                "sender_id",
                "text_sha256",
            )
        ):
            raise EvidenceError("ack_intent_mismatch")
        expected.append({field: message[field] for field in FIELDS})
    expected.sort(key=lambda row: int(row["seq"]))
    observed = [
        {field: str(row[field]) if field == "seq" else row[field] for field in FIELDS}
        for row in reference
    ]
    if (
        expected != observed
        or len({row["message_id"] for row in expected}) != 20
        or [int(row["seq"]) for row in expected]
        != list(range(int(expected[0]["seq"]), int(expected[0]["seq"]) + 20))
    ):
        raise EvidenceError("read_reference_not_this_run")


async def run(directory):
    run_id = str(uuid4())
    result = {
        "status": "incomplete",
        "run_id": run_id,
        "resumed": False,
        "pause_requested": False,
        "recovery_required": False,
    }
    attempts = []
    plan = [
        {
            "client_message_id": str(uuid4()),
            "conversation_id": ROOM,
            "sender_id": ACTOR,
            "text_sha256": sha256(f"replica-lag:{run_id}:{i}".encode()).hexdigest(),
        }
        for i in range(20)
    ]
    write_new(
        directory / "manifest.json",
        {
            "run_id": run_id,
            "plan": plan,
            "limits": {"seconds": 120, "cleanup_seconds": 15, "messages": 20},
        },
    )
    try:
        async with asyncio.timeout(120):
            sample = await asyncio.to_thread(resource_snapshot)
            append(directory / "resources.jsonl", sample)
            if sample["status"] != "ok":
                raise EvidenceError("resource_preflight_failed")
            await asyncio.to_thread(history.admin_identity, "replica", 5441)
            if (
                await admin(
                    "SELECT pg_is_in_recovery(); SELECT pg_is_wal_replay_paused();"
                )
                != "t\nf"
            ):
                raise EvidenceError("replica_not_running_or_already_paused")
            http = LabHTTP()
            status, _, cookie = await http.request(
                "api_a", "/v1/dev/session", method="POST", body={"user": "user_a"}
            )
            if status != 200 or not cookie:
                raise EvidenceError("session_failed")
            # Own the requested pause before sending it: even an unknown admin reply must resume.
            try:
                result["pause_requested"] = True
                await admin("SELECT pg_wal_replay_pause();")
                if await admin("SELECT pg_get_wal_replay_pause_state();") != "paused":
                    raise EvidenceError("pause_not_confirmed")
                result["paused_at"] = now()
                for index, planned in enumerate(plan):
                    row = {
                        **planned,
                        "attempt_id": index,
                        "outcome": "unknown",
                        "ack_status": None,
                        "response_message_id": None,
                        "response_seq": None,
                    }
                    try:
                        code, data, uid = await asyncio.to_thread(
                            entry_request,
                            MESSAGE_PATH,
                            cookie,
                            {
                                "client_message_id": planned["client_message_id"],
                                "text": f"replica-lag:{run_id}:{index}",
                            },
                        )
                        if code not in (200, 201):
                            raise EvidenceError("message_not_acknowledged")
                        message = normalize(data["data"])
                        row.update(
                            outcome="acknowledged",
                            ack_status=code,
                            message=message,
                            response_message_id=message["message_id"],
                            response_seq=message["seq"],
                            pod_uid=uid,
                        )
                    finally:
                        attempts.append(row)
                        append(directory / "requests.jsonl", row)
                    await asyncio.sleep(0.2)
                snapshot = directory / "snapshot.json"
                await history.run(
                    SimpleNamespace(
                        command="snapshot",
                        actor_id=UUID(ACTOR),
                        conversation_id=UUID(ROOM),
                        window=20,
                        output=snapshot,
                    )
                )
                args = {
                    "command": "verify",
                    "snapshot": snapshot,
                    "replica_port": 5441,
                    "expect_route": "primary_fallback",
                }
                if not await history.run(
                    SimpleNamespace(
                        **args, fallback_limit=10, output=directory / "paused.json"
                    )
                ):
                    raise EvidenceError("lag_not_excluded")
                bind_reference(
                    attempts,
                    json.loads((directory / "paused.json").read_text())["reference"],
                )
                try:
                    await history.run(
                        SimpleNamespace(
                            **args,
                            fallback_limit=0,
                            output=directory / "zero-fallback.json",
                        )
                    )
                except history.ReadUnavailable as error:
                    if str(error) != "PRIMARY_FALLBACK_BUDGET_EXHAUSTED":
                        raise
                    write_new(
                        directory / "zero-fallback.json",
                        {"status": "expected_rejection", "reason": str(error)},
                    )
                    result["negative_control"] = "rejected"
                else:
                    raise EvidenceError("zero_fallback_should_reject")
            finally:
                await resume_owned_pause(result)
            if not result["resumed"]:
                raise EvidenceError("resume_unconfirmed")
            watermark = json.loads(snapshot.read_text())["required_lsn"]
            for _ in range(50):
                replay = await admin("SELECT pg_last_wal_replay_lsn()::text;")
                if history.lsn_number(replay) >= history.lsn_number(watermark):
                    break
                await asyncio.sleep(0.2)
            else:
                raise EvidenceError("catchup_timeout")
            if not await history.run(
                SimpleNamespace(
                    command="verify",
                    snapshot=snapshot,
                    replica_port=5441,
                    expect_route="replica",
                    fallback_limit=10,
                    output=directory / "caught-up.json",
                )
            ):
                raise EvidenceError("caughtup_read_mismatch")
            bind_reference(
                attempts,
                json.loads((directory / "caught-up.json").read_text())["reference"],
            )
            result["intent_bound"] = True
            sample = await asyncio.to_thread(resource_snapshot)
            append(directory / "resources.jsonl", sample)
            if sample["status"] != "ok":
                raise EvidenceError("resource_postflight_failed")
            result["status"] = "pass"
    except asyncio.CancelledError:
        result["error"] = "experiment_cancelled"
    except TimeoutError:
        result["error"] = "experiment_deadline_exceeded"
    except Exception as error:  # noqa: BLE001 -- Never print credential-bearing database errors.
        result["error_class"] = type(error).__name__
        result["error"] = (
            str(error) if isinstance(error, EvidenceError) else "experiment_incomplete"
        )
    write_new(
        directory / "final.json",
        {
            "run_id": run_id,
            "complete": result["status"] == "pass",
            "planned_count": 20,
            "attempts": attempts,
            "stop_reason": result.get("error"),
            "resumed": result["resumed"],
            "recovery_required": result["recovery_required"],
        },
    )
    write_new(directory / "result.json", result)
    print(json.dumps(result))
    return int(result["status"] != "pass")


async def run_with_signals(directory):
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, task.cancel)
    try:
        return await run(directory)
    finally:
        for name in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-replay-pause", action="store_true")
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    root = Path(__file__).resolve().parents[3] / ".artifacts/chat-distributed"
    if (
        not args.allow_replay_pause
        or directory == root
        or not directory.is_relative_to(root)
    ):
        parser.error(
            "Explicit pause opt-in and fresh artifact child directory required"
        )
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    return asyncio.run(run_with_signals(directory))


if __name__ == "__main__":
    raise SystemExit(main())
