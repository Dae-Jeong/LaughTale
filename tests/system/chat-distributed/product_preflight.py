"""Read-only functional-run guard for the approved A2/G2/F1/R1 lab."""

import json
import re
import subprocess
from datetime import UTC, datetime

CONTEXT = "k3d-laughtale-local"
NAMESPACE = "laughtale-chat-external"
CONTAINERS = (
    "k3d-laughtale-local-server-0",
    "laughtale-postgres-lab-primary-1",
    "laughtale-postgres-lab-replica-1",
    "laughtale-kafka-lab-kafka-1",
    "laughtale-redis-lab-redis-1",
)


def command(*args):
    return subprocess.check_output(args, text=True, timeout=10)


def kube(*args):
    return json.loads(
        command("kubectl", "--context", CONTEXT, "-n", NAMESPACE, *args, "-o", "json")
    )


def sample():
    expected = {"chat": 2, "chat-gateway": 2, "chat-fanout": 1, "chat-relay": 1}
    pods = kube("get", "pods")["items"]
    actual, identities = {}, {}
    for pod in pods:
        app = pod["metadata"].get("labels", {}).get("app")
        if app not in expected:
            continue
        actual[app] = actual.get(app, 0) + 1
        statuses = pod["status"].get("containerStatuses", [])
        assert pod["status"]["phase"] == "Running" and statuses
        assert all(
            row["ready"]
            and not row.get("lastState", {}).get("terminated", {}).get("reason")
            == "OOMKilled"
            for row in statuses
        )
        identities[pod["metadata"]["name"]] = [
            pod["metadata"]["uid"],
            sum(row["restartCount"] for row in statuses),
        ]
    assert actual == expected, "functional topology mismatch"
    assert not any(
        job["status"].get("active", 0) for job in kube("get", "jobs")["items"]
    )
    containers = json.loads(command("docker", "inspect", *CONTAINERS))
    assert all(
        row["State"]["Running"] and not row["State"]["OOMKilled"] for row in containers
    )
    memory = [
        json.loads(line)
        for line in command(
            "docker", "stats", "--no-stream", "--format", "{{json .}}", *CONTAINERS
        ).splitlines()
    ]
    assert {row["Name"] for row in memory} == set(CONTAINERS)
    for row in memory:
        assert float(row["MemPerc"].rstrip("%")) <= (
            85 if row["Name"] == CONTAINERS[0] else 95
        )
    free = int(
        re.search(r"free percentage:\s*(\d+)%", command("memory_pressure", "-Q"))[1]
    )
    assert free >= 10
    disk = int(
        command(
            "docker", "exec", CONTAINERS[3], "du", "-sk", "/var/lib/kafka/data"
        ).split()[0]
    )
    assert disk < 3 * 1024 * 1024
    db = json.loads(
        command(
            "docker",
            "exec",
            CONTAINERS[1],
            "psql",
            "-U",
            "postgres",
            "-d",
            "laughtale_chat",
            "-XAtc",
            "SELECT json_build_object('database',current_database(),'connections',(SELECT count(*) FROM pg_stat_activity),'max',current_setting('max_connections')::int,'pending',(SELECT count(*) FROM chat.message_outbox WHERE published_at IS NULL));",
        )
    )
    assert db["database"] == "laughtale_chat" and db["connections"] + 2 < db["max"] - 3
    assert db["pending"] == 0
    quota = kube("get", "resourcequota", "lab-budget")["status"]
    assert int(quota["used"]["pods"]) + 1 <= int(quota["hard"]["pods"])
    return {
        "at": datetime.now(UTC).isoformat(),
        "status": "ok",
        "purpose": "functional-only",
        "topology": actual,
        "identities": identities,
        "host_free_percent": free,
        "memory": [{"name": row["Name"], "percent": row["MemPerc"]} for row in memory],
        "kafka_disk_kib": disk,
        "database": db,
        "quota": quota,
    }


if __name__ == "__main__":
    print(json.dumps(sample(), indent=2))
