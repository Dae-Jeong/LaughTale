"""Sequential, guarded local A/B; output only allowlisted container metadata."""

import json
import os
import subprocess
import time

from lock_probe import DOCKER, ROOT, append, command, guard, pods

COMPOSE = DOCKER + [
    "compose",
    "-f",
    "tests/load/chat-internal/relay-drain.compose.yaml",
]
NAME = "laughtale-relay-drain-runner"


def main():
    baseline = pods()
    folder = ROOT / ".artifacts/relay-drain"
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / ("control-" + str(time.time_ns()) + ".jsonl")
    for variant, image in [
        ("before", "external-lab-v5"),
        ("after", "relay-heads-isolated-v1"),
    ]:
        env = {
            **os.environ,
            "RELAY_DRAIN_VARIANT": variant,
            "RELAY_DRAIN_IMAGE": "laughtale-chat:" + image,
        }
        append(log, {"variant": variant, "preflight": guard(baseline)})
        command(
            COMPOSE + ["up", "-d", "--no-build", "--force-recreate", "runner"], env=env
        )
        started = time.monotonic()
        try:
            while True:
                state = json.loads(command(DOCKER + ["inspect", NAME]))[0]
                snap = {
                    "variant": variant,
                    "elapsed": time.monotonic() - started,
                    "image_id": state["Image"],
                    "running": state["State"]["Running"],
                    "oom": state["State"]["OOMKilled"],
                    "exit": state["State"]["ExitCode"],
                    "nano_cpus": state["HostConfig"]["NanoCpus"],
                    "memory": state["HostConfig"]["Memory"],
                    "guard": guard(baseline),
                }
                append(log, snap)
                if snap["oom"] or snap["elapsed"] > 780:
                    raise RuntimeError("runner_resource_or_time_budget")
                if not snap["running"]:
                    print(command(DOCKER + ["logs", "--tail", "4", NAME]), flush=True)
                    if snap["exit"]:
                        raise RuntimeError("runner_failed")
                    break
                print(
                    command(DOCKER + ["logs", "--tail", "1", NAME]).strip(), flush=True
                )
                time.sleep(15)
        except BaseException:
            subprocess.run(
                DOCKER + ["stop", "--time", "15", NAME], timeout=25, check=False
            )
            raise
    print(json.dumps({"control_evidence": str(log)}), flush=True)


if __name__ == "__main__":
    main()
