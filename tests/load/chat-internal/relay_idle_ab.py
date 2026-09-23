"""Bounded idle-delay A/B/A; same instrumented image, original deployment restored."""

import asyncio
import json
import sys
import time
from uuid import uuid4

from cluster_probe import main as cluster_main
from lock_probe import KUBE, ROOT, append, command, cpu_stat, guard, pods, sql
from relay_cpu_ab import deployment, set_cpu

IMAGE = "laughtale-chat:relay-poll-v1"
ENV = "LAB_RELAY_IDLE_MS"


def lab_value():
    raw = json.loads(command(KUBE + ["get", "deployment", "chat-relay", "-o", "json"]))
    container = next(
        c for c in raw["spec"]["template"]["spec"]["containers"] if c["name"] == "relay"
    )
    return next((v for v in container.get("env", []) if v["name"] == ENV), None)


def profile_patch(image, milliseconds):
    variable = (
        {"name": ENV, "value": str(milliseconds)}
        if milliseconds is not None
        else {"name": ENV, "$patch": "delete"}
    )
    return {
        "spec": {
            "template": {
                "spec": {
                    "containers": [{"name": "relay", "image": image, "env": [variable]}]
                }
            }
        }
    }


def set_profile(image, milliseconds):
    command(
        KUBE
        + [
            "patch",
            "deployment",
            "chat-relay",
            "--type",
            "strategic",
            "-p",
            json.dumps(profile_patch(image, milliseconds)),
        ]
    )
    # All profile switches happen outside the workload, at the original CPU limit.
    set_cpu("200m")
    if deployment()["image"] != image:
        raise ValueError("profile_image_mismatch")
    value = lab_value()
    if (milliseconds is None and value is not None) or (
        milliseconds is not None and value != {"name": ENV, "value": str(milliseconds)}
    ):
        raise ValueError("profile_environment_mismatch")


def snapshot():
    relay = [
        (uid, name)
        for uid, (name, _) in pods().items()
        if name.startswith("chat-relay-")
    ]
    if len(relay) != 1:
        raise ValueError("one_relay_required")
    uid, name = relay[0]
    logs = command(KUBE + ["logs", name, "--tail=500"])
    stats = [
        r
        for line in logs.splitlines()
        if line.startswith("{")
        for r in [json.loads(line)]
        if r.get("event") == "relay.poll_stats"
    ]
    if not stats:
        raise ValueError("poll_stats_missing")
    return {
        "uid": uid,
        "stats": stats[-1],
        "cpu": cpu_stat(name),
        "sample_monotonic": time.monotonic(),
    }


def cost_delta(before, after):
    if before["uid"] != after["uid"]:
        raise ValueError("relay_changed")
    elapsed = after["stats"]["at_monotonic"] - before["stats"]["at_monotonic"]
    cpu_elapsed = after["sample_monotonic"] - before["sample_monotonic"]
    a, b = before["stats"]["outcomes"], after["stats"]["outcomes"]
    outcomes = {key: b.get(key, 0) - a.get(key, 0) for key in a.keys() | b.keys()}
    cpu = {key: after["cpu"][key] - value for key, value in before["cpu"].items()}
    if (
        elapsed <= 0
        or cpu_elapsed <= 0
        or any(v < 0 for v in (*outcomes.values(), *cpu.values()))
    ):
        raise ValueError("counter_reset_or_invalid_window")
    return {
        "outcomes": outcomes,
        "counter_window_seconds": elapsed,
        "empty_claims_per_second": outcomes.get("idle", 0) / elapsed,
        "claim_attempts_per_second": sum(outcomes.values()) / elapsed,
        "cpu_window_seconds": cpu_elapsed,
        "cpu_delta": cpu,
        "cpu_millicores": cpu["usage_usec"] / 1000 / cpu_elapsed,
    }


async def main():
    original, fanout = deployment(), deployment("fanout")
    if (
        original["image"] != "laughtale-chat:relay-heads-isolated-v1"
        or lab_value() is not None
    ):
        raise ValueError("unexpected_relay_baseline")
    if (
        original["resources"]["limits"]["cpu"] != "200m"
        or fanout["resources"]["limits"]["cpu"] != "200m"
    ):
        raise ValueError("unexpected_cpu_baseline")
    guard(pods())
    directory = ROOT / ".artifacts/relay-idle-ab" / str(uuid4())
    directory.mkdir(parents=True)
    append(
        directory / "control.jsonl",
        {"original": original, "fanout": fanout, "experiment_image": IMAGE},
    )
    print(str(directory), flush=True)
    all_passed = True
    try:
        for label, ms in (("A", 500), ("B", 100), ("A2", 500)):
            await asyncio.to_thread(set_profile, IMAGE, ms)
            baseline = pods()
            guard(baseline)
            if sql(
                "SELECT to_json(count(*)) FROM chat.message_outbox WHERE published_at IS NULL;"
            ):
                raise ValueError("outbox_not_drained")
            print(f"{label}: idle {ms}ms, no-load cost window", flush=True)
            idle_before = await asyncio.to_thread(snapshot)
            for _ in range(4):
                await asyncio.sleep(5)
                append(
                    directory / "idle-resources.jsonl",
                    await asyncio.to_thread(guard, baseline),
                )
            idle_after = await asyncio.to_thread(snapshot)
            idle = cost_delta(idle_before, idle_after)
            if idle["outcomes"].get("published", 0) or idle["outcomes"].get(
                "database_retry", 0
            ):
                raise ValueError("idle_window_not_idle")
            append(
                directory / "idle.jsonl",
                {
                    "label": label,
                    "before": idle_before,
                    "after": idle_after,
                    "cost": idle,
                },
            )
            existing = set((ROOT / ".artifacts/cluster-path").iterdir())
            await cluster_main(modes=("pod",), observer=snapshot)
            created = set((ROOT / ".artifacts/cluster-path").iterdir()) - existing
            if len(created) != 1:
                raise ValueError("run_identity")
            run_dir = created.pop() / "pod"
            proof = json.loads((run_dir / "proof.json").read_text())
            load = cost_delta(
                json.loads((run_dir / "cost-before.json").read_text()),
                json.loads((run_dir / "cost-after.json").read_text()),
            )
            row = {
                "label": label,
                "idle_ms": ms,
                "run_dir": str(run_dir),
                "idle_cost": idle,
                "load_cost": load,
                "proof": proof,
            }
            append(directory / "results.jsonl", row)
            all_passed = all_passed and proof["pass"]
            print(
                json.dumps(
                    {
                        "label": label,
                        "ack_p99": proof["ack_p99_seconds"],
                        "outbox_p99": proof["outbox_p99_seconds"],
                        "ws_p99": proof["ws_p99_seconds"],
                        "correct": proof["correct"],
                        "pass": proof["pass"],
                        "idle_empty_per_s": idle["empty_claims_per_second"],
                        "idle_cpu_m": idle["cpu_millicores"],
                        "load_empty_per_s": load["empty_claims_per_second"],
                        "load_cpu_m": load["cpu_millicores"],
                    }
                ),
                flush=True,
            )
            if not proof["correct"]:
                break
    finally:
        recovery = {}
        try:
            await asyncio.to_thread(set_profile, original["image"], None)
            recovery["relay_restored"] = (
                deployment() == original and lab_value() is None
            )
            recovery["fanout_unchanged"] = deployment("fanout") == fanout
            recovery["guard"] = guard(pods())
            recovery["pending"] = sql(
                "SELECT to_json(count(*)) FROM chat.message_outbox WHERE published_at IS NULL;"
            )
        except Exception as exc:  # noqa: BLE001 — no secret-bearing raw exceptions
            recovery["error_type"] = type(exc).__name__
        (directory / "restoration.json").write_text(json.dumps(recovery, indent=2))
        print(json.dumps({"restoration": recovery}), flush=True)
        if (
            not recovery.get("relay_restored")
            or not recovery.get("fanout_unchanged")
            or recovery.get("pending") != 0
            or "error_type" in recovery
        ):
            raise ValueError("restore_unverified")
    return all_passed


if __name__ == "__main__":
    try:
        sys.exit(0 if asyncio.run(main()) else 1)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "incomplete", "error_type": type(exc).__name__}))
        sys.exit(2)
