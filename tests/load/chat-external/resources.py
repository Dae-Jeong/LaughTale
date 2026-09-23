"""Read-only bounded local resource sampling; STOP only controls this experiment."""

import argparse
import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPException
from pathlib import Path

CONTAINERS = ("laughtale-postgres-lab-primary-1", "laughtale-postgres-lab-replica-1")
PORTS = (18082, 18087)
METRIC = re.compile(
    r"([a-zA-Z_:][a-zA-Z0-9_:]*)(\{.*\})?\s+([-+0-9.eE]+|[+-]?Inf|NaN)\Z"
)
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="([^"\\]*)"')


class SampleError(ValueError):
    pass


def command(args: list[str], deadline: float) -> str:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SampleError("sample_deadline")
    try:
        response = subprocess.run(
            args, capture_output=True, text=True, timeout=min(3, remaining), check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SampleError("command_unavailable_or_timed_out") from None
    if response.returncode != 0:
        raise SampleError("command_failed")
    return response.stdout


def memory(deadline: float) -> dict:
    # No -l/-p/-S: those options apply or simulate pressure and are forbidden here.
    text = command(["/usr/bin/memory_pressure"], deadline)
    found = re.search(r"System-wide memory free percentage:\s*(\d+)%", text)
    if found is None or not 0 <= int(found[1]) <= 100:
        raise SampleError("memory_percentage_unavailable")
    return {"free_percent": int(found[1])}


def swap(deadline: float) -> dict:
    text = command(["/usr/sbin/sysctl", "-n", "vm.swapusage"], deadline)
    values = re.findall(r"(total|used|free)\s*=\s*([0-9.]+)([KMG])", text)
    if len(values) != 3:
        raise SampleError("swap_shape")
    return {
        f"{name}_bytes": int(
            float(value) * {"K": 1024, "M": 1024**2, "G": 1024**3}[unit]
        )
        for name, value, unit in values
    }


def processes(deadline: float) -> list[dict]:
    found = []
    for port in PORTS:
        try:
            text = command(
                ["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                deadline,
            )
        except SampleError:
            raise SampleError(f"app_missing_or_unobservable_{port}") from None
        pids = set(text.split())
        if len(pids) != 1 or not next(iter(pids)).isdigit():
            raise SampleError(f"app_missing_or_ambiguous_{port}")
        pid = int(next(iter(pids)))
        fields = command(
            ["/bin/ps", "-p", str(pid), "-o", "pid=,pcpu=,rss="], deadline
        ).split()
        if len(fields) != 3 or int(fields[0]) != pid:
            raise SampleError(f"app_missing_or_unobservable_{port}")
        found.append(
            {
                "port": port,
                "pid": pid,
                "cpu_percent": float(fields[1]),
                "rss_bytes": int(fields[2]) * 1024,
            }
        )
    return found


def containers(deadline: float) -> list[dict]:
    template = '{{.Name}}|{{.State.Running}}|{{.State.OOMKilled}}|{{.RestartCount}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}'
    try:
        text = command(
            ["docker", "inspect", "--format", template, *CONTAINERS], deadline
        )
    except SampleError:
        raise SampleError("container_state_unavailable") from None
    result = []
    for line in text.splitlines():
        name, running, oom, restarts, project, role = line.split("|")
        if (
            name.removeprefix("/") not in CONTAINERS
            or project != "laughtale-postgres-lab"
            or role not in {"primary", "replica"}
            or running not in {"true", "false"}
            or oom not in {"true", "false"}
        ):
            raise SampleError("container_identity_mismatch")
        result.append(
            {
                "name": name.removeprefix("/"),
                "running": running == "true",
                "oom_killed": oom == "true",
                "restart_count": int(restarts),
            }
        )
    if {value["name"] for value in result} != set(CONTAINERS) or len(result) != 2:
        raise SampleError("container_missing")
    return result


def docker_stats(deadline: float) -> list[dict]:
    text = command(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}", *CONTAINERS],
        deadline,
    )
    keep = {"Name", "CPUPerc", "MemUsage", "MemPerc", "NetIO", "BlockIO", "PIDs"}
    rows = [json.loads(line) for line in text.splitlines() if line]
    if len(rows) != 2 or {row["Name"] for row in rows} != set(CONTAINERS):
        raise SampleError("container_stats_missing")
    return [{key: value for key, value in row.items() if key in keep} for row in rows]


def safe_metrics(raw: str) -> tuple[str, dict]:
    kept, names, dropped = [], set(), 0
    loop_seen = False
    for line in raw.splitlines():
        if not line or line.startswith("#"):
            continue
        match = METRIC.fullmatch(line)
        if not match:
            raise SampleError("metric_exposition_shape")
        name, labels, _ = match.groups()
        loop_seen = loop_seen or "event_loop" in name or "eventloop" in name
        if not name.startswith(("db_", "chat_ws_")):
            dropped += 1
            continue
        if labels:
            pairs = LABEL.findall(labels)
            if ",".join(f'{key}="{value}"' for key, value in pairs) != labels[1:-1]:
                raise SampleError("metric_labels_shape")
            allowed = all(
                (key == "role" and value == "primary")
                or (
                    key == "outcome"
                    and value
                    in {
                        "committed",
                        "rolled_back",
                        "failed",
                        "acquired",
                        "timeout",
                        "succeeded",
                    }
                )
                or (key == "le" and re.fullmatch(r"[0-9.eE+-]+|\+Inf", value))
                for key, value in pairs
            )
            if not allowed:
                dropped += 1
                continue
        kept.append(line)
        names.add(name)
    if not kept:
        raise SampleError("scoped_metrics_missing")
    return "\n".join(kept) + "\n", {
        "metric_names": sorted(names),
        "event_loop_metric_observed": loop_seen,
        "discarded_unscoped_series": dropped,
        "scope": "db_and_chat_ws_only; HTTP labels intentionally excluded",
    }


def metrics(deadline: float) -> tuple[str, dict]:
    connection = HTTPConnection(
        "127.0.0.1", 18082, timeout=max(0.01, min(3, deadline - time.monotonic()))
    )
    try:
        connection.request("GET", "/metrics", headers={"Accept": "text/plain"})
        response = connection.getresponse()
        if response.status != 200:
            raise SampleError("metrics_http_error")
        raw = response.read(4_000_001)
        if len(raw) > 4_000_000:
            raise SampleError("metrics_response_limit")
        return safe_metrics(raw.decode("utf-8"))
    finally:
        connection.close()


def stop_reasons(sample: dict, baseline: dict | None) -> list[str]:
    reasons = []
    if sample.get("memory", {}).get("free_percent", 100) < 10:
        reasons.append("host_memory_below_10_percent")
    if any(
        not row["running"] or row["oom_killed"] for row in sample.get("containers", [])
    ):
        reasons.append("target_container_stopped_or_oom")
    if baseline:
        if (
            "processes" in sample
            and "processes" in baseline
            and [(p["port"], p["pid"]) for p in sample["processes"]]
            != [(p["port"], p["pid"]) for p in baseline["processes"]]
        ):
            reasons.append("target_process_changed")
        if (
            "containers" in sample
            and "containers" in baseline
            and {p["name"]: p["restart_count"] for p in sample["containers"]}
            != {p["name"]: p["restart_count"] for p in baseline["containers"]}
        ):
            reasons.append("target_container_restarted")
    if any(
        error.startswith(
            (
                "app_missing",
                "container_state_unavailable",
                "container_missing",
                "container_identity",
            )
        )
        for error in sample["errors"].values()
    ):
        reasons.append("target_missing_or_unobservable")
    return reasons


def sample_resources(output: Path, seconds: int = 100) -> dict:
    if not 1 <= seconds <= 100:
        raise SampleError("duration_limit")
    repo = Path(__file__).resolve().parents[3]
    if not output.resolve().is_relative_to(repo / ".artifacts"):
        raise SampleError("ignored_artifact_path_required")
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "--", str(output / "resources.json")],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if ignored.returncode != 0:
        raise SampleError("artifact_path_not_ignored")
    # The load runner exclusively creates this directory. Attach after it starts.
    if not output.is_dir():
        raise SampleError("start_load_before_sampler")
    target = output / "resources.json"
    if target.exists():
        raise SampleError("resource_report_exists")
    start, samples = time.monotonic(), []
    deadline = start + seconds
    baseline = None
    with ThreadPoolExecutor(max_workers=6) as pool:
        index = 0
        while time.monotonic() < deadline:
            entry = {
                "at": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": time.monotonic() - start,
                "errors": {},
            }
            sample_deadline = min(deadline, time.monotonic() + 4)
            jobs = {
                name: pool.submit(function, sample_deadline)
                for name, function in (
                    ("memory", memory),
                    ("swap", swap),
                    ("processes", processes),
                    ("containers", containers),
                    ("docker_stats", docker_stats),
                    ("metrics", metrics),
                )
            }
            for name, future in jobs.items():
                try:
                    value = future.result()
                    if name == "metrics":
                        text, metadata = value
                        with (output / f"metrics-{index:03d}.prom").open(
                            "x", encoding="utf-8"
                        ) as file:
                            file.write(text)
                        entry[name] = metadata
                    else:
                        entry[name] = value
                except SampleError as exc:
                    entry["errors"][name] = str(exc)
                except (OSError, HTTPException, ValueError, KeyError, TypeError):
                    entry["errors"][name] = "sampling_unavailable"
            reasons = stop_reasons(entry, baseline)
            entry["stop_reasons"] = reasons
            samples.append(entry)
            baseline = entry if baseline is None else baseline
            if reasons:
                try:
                    with (output / "STOP").open("x", encoding="utf-8") as signal:
                        json.dump({"source": "resources", "reasons": reasons}, signal)
                except FileExistsError:
                    pass
                break
            index += 1
            remaining = min(deadline, start + index * 5) - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
    result = {
        "status": "incomplete"
        if any(sample["errors"] for sample in samples)
        else "collected",
        "interval_seconds": 5,
        "duration_limit_seconds": seconds,
        "samples": samples,
    }
    with target.open("x", encoding="utf-8") as file:
        json.dump(result, file, indent=2, sort_keys=True)
        file.write("\n")
    return {
        "status": result["status"],
        "sample_count": len(samples),
        "stop_requested": any(sample["stop_reasons"] for sample in samples),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--seconds", type=int, default=100)
    args = parser.parse_args(argv)
    try:
        result = sample_resources(args.output_dir, args.seconds)
    except (OSError, ValueError):
        print('{"status":"incomplete","issue":"sampler_setup_failed"}')
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "collected" and not result["stop_requested"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
