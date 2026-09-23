"""Three bounded fixed-CPU repeats; no retries and always restore Relay CPU."""

import argparse
import asyncio
import json
import signal
import sys
from uuid import uuid4

from e2e_probe import main as e2e
from lock_probe import ROOT, append, guard, pods, sql
from relay_cpu_ab import deployment, set_cpu


def classify(proof):
    if not proof:
        return "incomplete_evidence"
    if not proof.get("forward_alive"):
        return "incomplete_path"
    if not proof.get("correct"):
        return "integrity_or_arrival_failure"
    return "pass" if proof.get("pass") else "latency_failure"


async def main(*, http_keepalive=True, rate=50):
    if rate not in (25, 50):
        raise ValueError("bounded_rate_required")
    original = await asyncio.to_thread(deployment)
    fanout = await asyncio.to_thread(deployment, "fanout")
    if any(
        s["resources"]["limits"]["cpu"] != "200m" or s["replicas"] != 1
        for s in (original, fanout)
    ):
        raise ValueError("unexpected_baseline")
    await asyncio.to_thread(guard, pods())
    folder = ROOT / ".artifacts/path-repeat" / str(uuid4())
    folder.mkdir(parents=True)
    append(folder / "control.jsonl", {"original": original, "fanout": fanout})
    print(str(folder), flush=True)
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await asyncio.to_thread(set_cpu, "400m")
        wanted = json.loads(json.dumps(original))
        wanted["resources"]["limits"]["cpu"] = "400m"
        all_passed = True
        for index in range(1, 4):
            if (await asyncio.to_thread(deployment)) != wanted or (
                await asyncio.to_thread(deployment, "fanout")
            ) != fanout:
                raise ValueError("configuration_changed")
            pending = await asyncio.to_thread(
                sql,
                "SELECT to_json(count(*)) FROM chat.message_outbox WHERE published_at IS NULL;",
            )
            if pending:
                raise ValueError("outbox_not_drained")
            before = set((ROOT / ".artifacts/chat-lock").iterdir())
            error = None
            try:
                await asyncio.wait_for(
                    e2e(rate, http_keepalive=http_keepalive, http_keepalive_expiry=1),
                    210,
                )
            except Exception as exc:  # noqa: BLE001 — no credentials in exception text
                error = type(exc).__name__
            created = set((ROOT / ".artifacts/chat-lock").iterdir()) - before
            proof, run_dir = None, None
            if len(created) == 1:
                run_dir = created.pop()
                path = run_dir / "e2e-proof.json"
                if path.exists():
                    proof = json.loads(path.read_text())
            row = {
                "repeat": index,
                "run_dir": str(run_dir),
                "error_type": error,
                "classification": classify(proof),
                "proof": proof,
            }
            append(folder / "results.jsonl", row)
            print(json.dumps(row), flush=True)
            all_passed = all_passed and error is None and classify(proof) == "pass"
            if error or classify(proof) not in ("pass", "latency_failure"):
                break
        return all_passed
    finally:
        # Restore even when measurement or its final read fails.
        recovery = {}
        try:
            await asyncio.to_thread(set_cpu, original["resources"]["limits"]["cpu"])
            recovery["relay_restored"] = (
                await asyncio.to_thread(deployment)
            ) == original
            recovery["fanout_unchanged"] = (
                await asyncio.to_thread(deployment, "fanout")
            ) == fanout
            recovery["guard"] = await asyncio.to_thread(guard, pods())
            recovery["pending"] = await asyncio.to_thread(
                sql,
                "SELECT to_json(count(*)) FROM chat.message_outbox WHERE published_at IS NULL;",
            )
        except Exception as exc:  # noqa: BLE001
            recovery["error_type"] = type(exc).__name__
        (folder / "restoration.json").write_text(json.dumps(recovery, indent=2))
        print(json.dumps({"restoration": recovery}), flush=True)
        if (
            not recovery.get("relay_restored")
            or not recovery.get("fanout_unchanged")
            or "error_type" in recovery
            or recovery.get("pending") != 0
        ):
            raise ValueError("restore_unverified")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-http-keepalive", action="store_true")
    parser.add_argument("--rate", type=int, choices=(25, 50), default=50)
    args = parser.parse_args()
    sys.exit(
        0
        if asyncio.run(main(http_keepalive=not args.no_http_keepalive, rate=args.rate))
        else 1
    )
