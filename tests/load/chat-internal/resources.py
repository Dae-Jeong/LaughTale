"""Read-only resource sampling for the bounded local chat demonstration."""

import argparse
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

CONTAINERS = (
    "k3d-laughtale-local-server-0",
    "laughtale-kafka-lab-kafka-1",
    "laughtale-postgres-lab-primary-1",
    "laughtale-postgres-lab-replica-1",
)
CONTEXT = "k3d-laughtale-local"
NAMESPACE = "laughtale-chat-external"
OUTBOX_SQL = """
BEGIN READ ONLY;
SET LOCAL statement_timeout = '2s';
SELECT json_build_object(
  'total', count(*),
  'pending', count(*) FILTER (WHERE published_at IS NULL),
  'published', count(*) FILTER (WHERE published_at IS NOT NULL),
  'oldest_pending_seconds', extract(epoch FROM clock_timestamp() -
    min(created_at) FILTER (WHERE published_at IS NULL))
) FROM chat.message_outbox;
COMMIT;
"""


def command(*args: str) -> str:
    return subprocess.run(
        args, capture_output=True, text=True, check=True, timeout=5
    ).stdout


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def sample(*, distributed: bool = False) -> tuple[dict, dict]:
    targets = CONTAINERS + (("laughtale-redis-lab-redis-1",) if distributed else ())
    pressure = command("memory_pressure", "-Q")
    match = re.search(r"free percentage:\s*(\d+)%", pressure)
    if match is None:
        raise ValueError("Host memory sample missing")
    free = int(match.group(1))
    states = json.loads(command("docker", "inspect", *targets))
    identities = {}
    for state in states:
        if not state["State"]["Running"] or state["State"]["OOMKilled"]:
            raise ValueError("Target container stopped or OOM")
        identities[state["Name"]] = [state["Id"], state["RestartCount"]]
    containers = []
    for line in command(
        "docker", "stats", "--no-stream", "--format", "{{json .}}", *targets
    ).splitlines():
        value = json.loads(line)
        containers.append(
            {
                "name": value["Name"],
                "memory_percent": float(value["MemPerc"].rstrip("%")),
                "cpu_percent": float(value["CPUPerc"].rstrip("%")),
            }
        )
    if {row["name"] for row in containers} != set(targets):
        raise ValueError("Container stats missing")
    pods = json.loads(
        command(
            "kubectl",
            "--context",
            CONTEXT,
            "-n",
            NAMESPACE,
            "get",
            "pods",
            "-l",
            "app in (chat,chat-relay,chat-gateway,chat-fanout)"
            if distributed
            else "app in (chat,chat-relay)",
            "-o",
            "json",
        )
    )["items"]
    expected = {"chat": 2, "chat-relay": 1}
    if distributed:
        expected.update({"chat-gateway": 2, "chat-fanout": 2})
    actual = {}
    for pod in pods:
        name = pod["metadata"]["labels"]["app"]
        actual[name] = actual.get(name, 0) + 1
    if actual != expected:
        raise ValueError("Expected fixed application replica counts")
    for pod in pods:
        statuses = pod["status"].get("containerStatuses", [])
        if (
            pod["status"].get("phase") != "Running"
            or not statuses
            or not all(item["ready"] for item in statuses)
        ):
            raise ValueError("Application Pod unavailable")
        identities[pod["metadata"]["name"]] = [
            pod["metadata"]["uid"],
            sum(item["restartCount"] for item in statuses),
        ]
    raw = command(
        "docker",
        "exec",
        CONTAINERS[2],
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
        OUTBOX_SQL,
    )
    outbox = json.loads(raw.strip())
    disk = int(
        command(
            "docker", "exec", CONTAINERS[1], "du", "-sk", "/var/lib/kafka/data"
        ).split()[0]
    )
    reasons = []
    if free < 10:
        reasons.append("host_memory_low")
    if containers[0]["memory_percent"] > 85:
        reasons.append("k3d_memory_limit")
    if any(row["memory_percent"] > 95 for row in containers[1:]):
        reasons.append("dependency_memory_limit")
    if disk >= 3 * 1024 * 1024:
        reasons.append("kafka_disk_budget")
    return {
        "sampled_at": datetime.now(timezone.utc).isoformat(),
        "status": "stop" if reasons else "ok",
        "reason": ",".join(reasons) or None,
        "host_free_percent": free,
        "containers": containers,
        "outbox": outbox,
        "kafka_volume_kib": disk,
    }, identities


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--seconds", type=int, default=85)
    args = parser.parse_args()
    if not 5 <= args.seconds <= 100 or not args.run_dir.is_dir():
        parser.error("Use an existing run directory and 5–100 seconds")
    samples = args.run_dir / "resources.jsonl"
    baseline = None
    deadline = time.monotonic() + args.seconds
    with samples.open("x", encoding="utf-8") as output:
        while time.monotonic() < deadline:
            try:
                value, identities = sample()
                if baseline is None:
                    baseline = identities
                elif identities != baseline:
                    value.update(status="stop", reason="target_replaced_or_restarted")
            except (subprocess.SubprocessError, ValueError, KeyError, OSError):
                value = {
                    "sampled_at": datetime.now(timezone.utc).isoformat(),
                    "status": "incomplete",
                    "reason": "required_resource_sample_failed",
                }
            output.write(json.dumps(value) + "\n")
            output.flush()
            write_json(args.run_dir / "infra.json", value)
            if value["status"] != "ok":
                (args.run_dir / "STOP").touch(exist_ok=True)
                print(json.dumps(value))
                raise SystemExit(1)
            print(json.dumps(value), flush=True)
            time.sleep(min(5, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    main()
