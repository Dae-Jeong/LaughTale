"""Coordinate only the external capacity lab; a stale heartbeat stops the generator."""

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from uuid import uuid4

from app import CONTEXT, KUBE, NAMESPACE, guarded_cluster, sql
from capacity import EXTERNAL, MOUNT
from lab import ENV_FILE, PRIVATE, ROOT, guarded_engine, run

sys.path.insert(0, str(ROOT / "tests/load/chat-capacity"))
from ledger import resource_failure

PREFIX = KUBE + ["-n", NAMESPACE]
TARGETS = [
    "laughtale-capacity-primary-1",
    "laughtale-capacity-replica-1",
    "laughtale-capacity-kafka-1",
    "laughtale-capacity-redis-1",
    "k3d-laughtale-capacity-server-0",
]


def capture(argv, **kwargs):
    return run(argv, capture_output=True, text=True, timeout=20, **kwargs).stdout


def pods():
    return json.loads(capture(PREFIX + ["get", "pods", "-o", "json"]))["items"]


def baseline_pods(items):
    return {
        item["metadata"]["uid"]: tuple(
            status["restartCount"]
            for status in item["status"].get("containerStatuses", [])
        )
        for item in items
        if item["metadata"].get("labels", {}).get("app") != "traffic-generator"
    }


def sample(baseline):
    guarded_engine()
    memory = capture(["memory_pressure", "-Q"])
    free = int(re.search(r"free percentage: (\d+)%", memory)[1])
    env = dict(os.environ, LIMA_HOME=str(MOUNT / "lima"))
    disk = capture(
        ["limactl", "shell", "--workdir", "/", "laughtale-capacity", "df", "-Pk", "/"],
        env=env,
    )
    guest_free = int(disk.splitlines()[-1].split()[3]) * 1024
    containers = json.loads(
        capture(["docker", "--context", CONTEXT, "inspect", *TARGETS])
    )
    current = pods()
    healthy = baseline_pods(current) == baseline and all(
        item["State"]["Running"]
        and not item["State"]["OOMKilled"]
        and item["RestartCount"] == 0
        for item in containers
    )
    healthy = healthy and all(
        item["status"].get("phase") == "Running"
        and bool(item["status"].get("containerStatuses"))
        and all(status["ready"] for status in item["status"]["containerStatuses"])
        for item in current
        if item["metadata"].get("labels", {}).get("app") != "traffic-generator"
    )
    value = {
        "time": time.time(),
        "host_free_percent": free,
        "internal_free_gib": shutil.disk_usage(ROOT).free / 1024**3,
        "external_free_gib": shutil.disk_usage(EXTERNAL).free / 1024**3,
        "guest_free_gib": guest_free / 1024**3,
        "healthy": healthy,
    }
    value["pod_usage"] = capture(PREFIX + ["top", "pods", "--no-headers"]).splitlines()
    return value


def apply(item):
    capture(KUBE + ["apply", "-f", "-"], input=json.dumps(item))


def prepare(run_id, count, rate):
    # Session issuance still goes through the real API, locally inside its Pod.
    cookie = capture(
        PREFIX
        + [
            "exec",
            "deployment/chat",
            "--",
            "python",
            "-c",
            "import urllib.request,http.cookies; r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:18082/v1/dev/session',data=b'{\"user\":\"user_a\"}',headers={'Origin':'http://127.0.0.1:18083','Content-Type':'application/json'})); c=http.cookies.SimpleCookie();c.load(r.headers['Set-Cookie']);print(c['chat_session'].value)",
        ]
    ).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", cookie):
        raise RuntimeError("Invalid synthetic session response")
    gateway = next(
        item["status"]["podIP"]
        for item in pods()
        if item["metadata"].get("labels", {}).get("app") == "chat-gateway"
    )
    sql(f"""BEGIN; SET LOCAL ROLE chat_owner;
        INSERT INTO chat.conversations(id) VALUES ('{run_id}');
        INSERT INTO chat.members(conversation_id,user_id) VALUES
          ('{run_id}','00000000-0000-4000-8000-000000000001'),
          ('{run_id}','00000000-0000-4000-8000-000000000002'); COMMIT;""")
    passwords = dict(line.split("=", 1) for line in ENV_FILE.read_text().splitlines())
    tokens = json.loads((PRIVATE / "app.json").read_text())
    name = "capacity-" + run_id
    capture(
        PREFIX + ["create", "-f", "-"],
        input=json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": name, "namespace": NAMESPACE},
                "immutable": True,
                "stringData": {
                    "SESSION_COOKIE": cookie,
                    "READER_PASSWORD": passwords["CHAT_READER_PASSWORD"],
                    "LAB_INGRESS_TOKEN": tokens["ingress"],
                    "GATEWAY_IP": gateway,
                },
            }
        ),
    )
    source = ROOT / "tests/load/chat-capacity"
    data = {
        filename: (source / filename).read_text()
        for filename in ("pilot.py", "ledger.py")
    }
    config_name = (
        "capacity-source-"
        + hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]
    )
    apply(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": config_name, "namespace": NAMESPACE},
            "data": data,
        }
    )
    apply(
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": "capacity-evidence", "namespace": NAMESPACE},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": "local-path",
                "resources": {"requests": {"storage": "128Gi"}},
            },
        }
    )
    apply(
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "capacity-generator", "namespace": NAMESPACE},
            "spec": {
                "podSelector": {"matchLabels": {"app": "traffic-generator"}},
                "policyTypes": ["Egress"],
                "egress": [
                    {
                        "to": [
                            {"podSelector": {"matchLabels": {"app": "chat-gateway"}}}
                        ],
                        "ports": [{"protocol": "TCP", "port": 18082}],
                    },
                    {
                        "to": [
                            {"ipBlock": {"cidr": "172.28.0.2/32"}},
                            {"ipBlock": {"cidr": "172.28.0.3/32"}},
                        ],
                        "ports": [{"protocol": "TCP", "port": 5432}],
                    },
                    {
                        "to": [{"ipBlock": {"cidr": "172.28.0.5/32"}}],
                        "ports": [{"protocol": "TCP", "port": 9092}],
                    },
                ],
            },
        }
    )
    apply(
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "gateway-from-generator", "namespace": NAMESPACE},
            "spec": {
                "podSelector": {"matchLabels": {"app": "chat-gateway"}},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [
                            {
                                "podSelector": {
                                    "matchLabels": {"app": "traffic-generator"}
                                }
                            }
                        ],
                        "ports": [{"protocol": "TCP", "port": 18082}],
                    }
                ],
            },
        }
    )
    apply(
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": name, "namespace": NAMESPACE},
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": math.ceil(count / rate) + 480,
                "template": {
                    "metadata": {"labels": {"app": "traffic-generator"}},
                    "spec": {
                        "restartPolicy": "Never",
                        "automountServiceAccountToken": False,
                        "terminationGracePeriodSeconds": 20,
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 10001,
                            "runAsGroup": 10001,
                            "fsGroup": 10001,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": "generator",
                                "image": "laughtale-chat:capacity-v1",
                                "imagePullPolicy": "Never",
                                "command": [
                                    "python",
                                    "/opt/pilot/pilot.py",
                                    "--run-id",
                                    run_id,
                                    "--count",
                                    str(count),
                                    "--rate",
                                    str(rate),
                                ],
                                "envFrom": [{"secretRef": {"name": name}}],
                                "resources": {
                                    "requests": {"cpu": "100m", "memory": "128Mi"},
                                    "limits": {"cpu": "1", "memory": "512Mi"},
                                },
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "volumeMounts": [
                                    {
                                        "name": "source",
                                        "mountPath": "/opt/pilot",
                                        "readOnly": True,
                                    },
                                    {"name": "evidence", "mountPath": "/evidence"},
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "source", "configMap": {"name": config_name}},
                            {
                                "name": "evidence",
                                "persistentVolumeClaim": {
                                    "claimName": "capacity-evidence"
                                },
                            },
                        ],
                    },
                },
            },
        }
    )
    return name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--rate", type=float, default=10)
    parser.add_argument(
        "--stop-after",
        type=float,
        help="Inject a coordinator stop after this many seconds",
    )
    args = parser.parse_args()
    if not 1 <= args.count <= 100000 or not 1 <= args.rate <= 2000:
        parser.error("Bounded pilot limits exceeded")
    guarded_engine()
    guarded_cluster()
    baseline = baseline_pods(pods())
    preflight = sample(baseline)
    if reason := resource_failure(preflight):
        raise RuntimeError("Preflight refused: " + reason)
    run_id = str(uuid4())
    folder = ROOT / ".artifacts/chat-capacity" / run_id
    folder.mkdir()
    name = prepare(run_id, args.count, args.rate)
    print(
        f"Pilot {run_id}: {args.count} at {args.rate}/s; isolated Job {name}",
        flush=True,
    )
    deadline = time.monotonic() + args.count / args.rate + 480
    monitor_started = time.monotonic()
    latched_reason = None
    final = None
    with (folder / "resources.jsonl").open("x") as evidence:
        while time.monotonic() < deadline:
            value = sample(baseline)
            latched_reason = latched_reason or resource_failure(value)
            if (
                args.stop_after is not None
                and time.monotonic() - monitor_started >= args.stop_after
            ):
                latched_reason = latched_reason or "injected_coordinator_stop"
            value["stop_reason"] = latched_reason
            evidence.write(json.dumps(value) + "\n")
            evidence.flush()
            reason = latched_reason
            items = json.loads(
                capture(
                    PREFIX + ["get", "pods", "-l", "job-name=" + name, "-o", "json"]
                )
            )["items"]
            if items:
                pod = items[0]
                phase = pod["status"]["phase"]
                if phase in {"Succeeded", "Failed"}:
                    raw = capture(
                        PREFIX + ["logs", pod["metadata"]["name"], "--tail=1"]
                    )
                    try:
                        final = json.loads(raw)
                    except ValueError:
                        final = {
                            "status": "incomplete",
                            "reason": "missing_final_generator_evidence",
                        }
                    break
                if phase == "Running":
                    target = "STOP" if reason else "lease"
                    # Missing folder during startup is safe: the initial lease is only60s.
                    subprocess.run(
                        PREFIX
                        + [
                            "exec",
                            pod["metadata"]["name"],
                            "--",
                            "touch",
                            f"/evidence/{run_id}/{target}",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                    )
            if reason:
                print("Safety stop: " + reason, flush=True)
                # Latch the stop; observe the final result without renewing the lease.
            time.sleep(5)
    final = final or {
        "status": "incomplete",
        "reason": "coordinator_stopped; inspect retained PVC ledger",
    }
    final["run_id"] = run_id
    final["coordinator_stop_reason"] = latched_reason
    if latched_reason:
        final["status"] = "fail"
    (folder / "result.json").write_text(json.dumps(final, indent=2) + "\n")
    print(json.dumps(final, indent=2), flush=True)
    return final.get("status") == "pass"


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
