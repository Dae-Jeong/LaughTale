"""Approved local Relay CPU-only A/B/A, with restoration and scoped evidence."""

import argparse
import asyncio
import json
import subprocess
import sys
import time
from uuid import uuid4

from lock_probe import KUBE, ROOT, append, command, cpu_stat, guard, pods, sql


def deployment(component="relay"):
    raw = json.loads(
        command(KUBE + ["get", "deployment", "chat-" + component, "-o", "json"])
    )
    container = next(
        c
        for c in raw["spec"]["template"]["spec"]["containers"]
        if c["name"] == component
    )
    return {
        "replicas": raw["spec"]["replicas"],
        "image": container["image"],
        "resources": container["resources"],
    }


def set_cpu(value, component="relay"):
    patch = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": component, "resources": {"limits": {"cpu": value}}}
                    ]
                }
            }
        }
    }
    command(
        KUBE
        + [
            "patch",
            "deployment",
            "chat-" + component,
            "--type",
            "strategic",
            "-p",
            json.dumps(patch),
        ]
    )
    subprocess.run(
        KUBE + ["rollout", "status", "deployment/chat-" + component, "--timeout=90s"],
        check=True,
        timeout=100,
        capture_output=True,
    )
    # Rollout completion can precede old Pod deletion / readiness settling.
    deadline, previous, stable = time.monotonic() + 60, None, 0
    while time.monotonic() < deadline:
        try:
            current = pods()
        except ValueError:
            current = None
        if current and len(current) == 6 and current == previous:
            stable += 1
            if stable >= 5:
                return
        else:
            stable = 0
        previous = current
        time.sleep(2)
    raise ValueError("rollout_not_stable")


def cpus(baseline):
    return {name: cpu_stat(name) for name, _ in baseline.values()}


async def main(component="relay"):
    original = deployment(component)
    relay_original = deployment()
    if original["resources"]["limits"]["cpu"] != "200m" or original["replicas"] != 1:
        raise ValueError("unexpected_baseline")
    guard(pods())
    if relay_original["resources"]["limits"]["cpu"] != "200m":
        raise ValueError("relay_baseline_required")
    folder = ROOT / (".artifacts/" + component + "-cpu-ab") / str(uuid4())
    folder.mkdir(parents=True)
    append(
        folder / "control.jsonl",
        {
            "component": component,
            "original": original,
            "relay_original": relay_original,
        },
    )
    print(str(folder), flush=True)
    process = None
    try:
        if component == "fanout":
            await asyncio.to_thread(set_cpu, "400m")
        for label, limit in (("A", "200m"), ("B", "400m"), ("A2", "200m")):
            if deployment(component)["resources"]["limits"]["cpu"] != limit:
                await asyncio.to_thread(set_cpu, limit, component)
            baseline = pods()
            guard(baseline)
            state = deployment(component)
            wanted = json.loads(json.dumps(original))
            wanted["resources"]["limits"]["cpu"] = limit
            if state != wanted:
                raise ValueError("non_cpu_configuration_changed")
            before = await asyncio.to_thread(cpus, baseline)
            existing = set((ROOT / ".artifacts/chat-lock").iterdir())
            started = time.monotonic()
            with (folder / (label + ".log")).open("w") as output:
                process = await asyncio.to_thread(
                    subprocess.Popen,
                    [
                        sys.executable,
                        "tests/load/chat-internal/e2e_probe.py",
                        "--rate",
                        "50",
                    ],
                    cwd=ROOT,
                    stdout=output,
                    stderr=output,
                )
                while process.poll() is None:
                    resources = await asyncio.to_thread(guard, baseline)
                    backlog = await asyncio.to_thread(
                        sql,
                        "SELECT json_build_object('pending',count(*),'oldest_seconds',extract(epoch FROM clock_timestamp()-min(created_at))) FROM chat.message_outbox WHERE published_at IS NULL;",
                    )
                    append(
                        folder / "samples.jsonl",
                        {
                            "label": label,
                            "elapsed": time.monotonic() - started,
                            "resources": resources,
                            "outbox": backlog,
                        },
                    )
                    if time.monotonic() - started > 210:
                        raise ValueError("experiment_deadline")
                    await asyncio.sleep(5)
            exit_code = process.returncode
            process = None
            after = await asyncio.to_thread(cpus, baseline)
            created = set((ROOT / ".artifacts/chat-lock").iterdir()) - existing
            if len(created) != 1:
                raise ValueError("ambiguous_run")
            run_folder = created.pop()
            if not (run_folder / "e2e-proof.json").exists():
                append(
                    folder / "results.jsonl",
                    {
                        "label": label,
                        "component": component,
                        "status": "incomplete",
                        "exit": exit_code,
                        "run_folder": str(run_folder),
                    },
                )
                raise ValueError("e2e_evidence_incomplete")
            proof = json.loads((run_folder / "e2e-proof.json").read_text())
            requests = [
                json.loads(x)
                for x in (run_folder / "requests.jsonl").read_text().splitlines()
            ]
            row = {
                "label": label,
                "component": component,
                "deployment": state,
                "exit": exit_code,
                "run_folder": str(run_folder),
                "proof": proof,
                "cpu_delta": {
                    name: {key: after[name][key] - value for key, value in data.items()}
                    for name, data in before.items()
                },
                "skipped_reasons": {
                    reason: sum(r.get("reason") == reason for r in requests)
                    for reason in ("inflight_limit", "dispatch_lag")
                },
            }
            append(folder / "results.jsonl", row)
            print(
                json.dumps(
                    {
                        k: v
                        for k, v in row.items()
                        if k not in {"cpu_delta", "deployment"}
                    }
                ),
                flush=True,
            )
            if (
                exit_code not in (0, 1)
                or proof["failures"]
                or proof["kafka_invalid"]
                or proof["kafka_duplicates"]
            ):
                raise ValueError("safety_or_integrity_failure")
            if sql(
                "SELECT to_json(count(*)) FROM chat.message_outbox WHERE published_at IS NULL;"
            ):
                raise ValueError("backlog_not_drained")
    finally:
        recovery = {"errors": {}, "states": {}}
        try:
            if process is not None and process.poll() is None:
                process.terminate()
                await asyncio.to_thread(process.wait, timeout=30)
        except Exception as exc:  # noqa: BLE001 — preserve all restoration attempts
            recovery["errors"]["runner"] = type(exc).__name__
        originals = {component: original, "relay": relay_original}
        for target, wanted in originals.items():
            try:
                if deployment(target)["resources"]["limits"]["cpu"] != "200m":
                    await asyncio.to_thread(set_cpu, "200m", target)
                recovery["states"][target] = deployment(target)
                if recovery["states"][target] != wanted:
                    recovery["errors"][target] = "configuration_mismatch"
            except Exception as exc:  # noqa: BLE001 — one failure must not skip another target
                recovery["errors"][target] = type(exc).__name__
        try:
            recovery["guard"] = guard(pods())
        except Exception as exc:  # noqa: BLE001 — retain incomplete recovery evidence
            recovery["errors"]["guard"] = type(exc).__name__
        recovery["restored"] = not recovery["errors"]
        append(folder / "control.jsonl", recovery)
        print(
            json.dumps({"restored": recovery["restored"], "folder": str(folder)}),
            flush=True,
        )
        if not recovery["restored"]:
            raise RuntimeError("restore_unverified_see_control_evidence")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", choices=("relay", "fanout"), default="relay")
    asyncio.run(main(parser.parse_args().component))
