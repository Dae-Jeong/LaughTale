"""Render/deploy the existing chat topology only to the guarded capacity cluster."""

import argparse
import json
import os
import secrets
import subprocess

from lab import CONTEXT, ENV_FILE, PRIVATE, ROOT, guarded_engine, run

NAMESPACE = "laughtale-chat-external"
KUBE = [
    "kubectl",
    "--kubeconfig",
    str(PRIVATE / "kubeconfig.yaml"),
    "--context",
    "k3d-laughtale-capacity",
]
PG = [
    "docker",
    "--context",
    CONTEXT,
    "exec",
    "-i",
    "laughtale-capacity-primary-1",
    "psql",
    "-X",
    "-v",
    "ON_ERROR_STOP=1",
    "-U",
    "postgres",
    "-d",
    "laughtale_chat",
]


def sql(value):
    return run(
        PG + ["-Atq"], input=value, text=True, capture_output=True
    ).stdout.strip()


def guarded_cluster():
    server = run(
        KUBE
        + [
            "config",
            "view",
            "--minify",
            "-o",
            "jsonpath={.clusters[0].cluster.server}",
        ],
        text=True,
        capture_output=True,
    ).stdout
    if server != "https://127.0.0.1:26447":
        raise RuntimeError("Unexpected Kubernetes API address")
    query = ["get", "namespace", "kube-system", "-o", "jsonpath={.metadata.uid}"]
    actual = run(KUBE + query, text=True, capture_output=True).stdout
    expected = run(
        [
            "docker",
            "--context",
            CONTEXT,
            "exec",
            "k3d-laughtale-capacity-server-0",
            "kubectl",
        ]
        + query,
        text=True,
        capture_output=True,
    ).stdout
    if not actual or actual != expected:
        raise RuntimeError("Capacity cluster identity mismatch")


def bootstrap():
    if (
        sql("SELECT current_database() || ':' || pg_is_in_recovery()::text")
        != "laughtale_chat:false"
    ):
        raise RuntimeError("Unexpected primary identity")
    if not sql("SELECT to_regclass('chat.alembic_version')"):
        migration = run(
            [str(ROOT / "services/chat/.venv/bin/alembic"), "upgrade", "head", "--sql"],
            cwd=ROOT / "services/chat",
            text=True,
            capture_output=True,
        ).stdout
        sql(migration)
    if sql("SELECT version_num FROM chat.alembic_version") != "0002":
        raise RuntimeError("Review unexpected migration revision")
    sql("""BEGIN; SET LOCAL ROLE chat_owner;
      INSERT INTO chat.users(id,display_name) VALUES
        ('00000000-0000-4000-8000-000000000001','User A'),
        ('00000000-0000-4000-8000-000000000002','User B') ON CONFLICT DO NOTHING;
      INSERT INTO chat.conversations(id) VALUES ('00000000-0000-4000-8000-000000000010')
        ON CONFLICT DO NOTHING;
      INSERT INTO chat.members(conversation_id,user_id)
        SELECT '00000000-0000-4000-8000-000000000010'::uuid,id FROM chat.users
        WHERE id IN ('00000000-0000-4000-8000-000000000001','00000000-0000-4000-8000-000000000002')
        ON CONFLICT DO NOTHING;
      COMMIT;""")
    print("Capacity schema and synthetic DM seed verified.")


def translate(value):
    if isinstance(value, dict):
        return {key: translate(item) for key, item in value.items()}
    if isinstance(value, list):
        return [translate(item) for item in value]
    if isinstance(value, str):
        return value.replace("172.22.0.", "172.28.0.")
    return value


def adapt(items):
    items = translate(items)
    for item in items:
        kind = item["kind"]
        if kind != "Namespace":
            item["metadata"]["namespace"] = NAMESPACE
        if kind == "ResourceQuota":
            item["spec"]["hard"].update(
                {
                    "pods": "12",
                    "requests.cpu": "1500m",
                    "requests.memory": "2Gi",
                    "limits.cpu": "4",
                    "limits.memory": "3Gi",
                }
            )
        if kind == "Deployment":
            name = item["metadata"]["name"]
            item["spec"]["replicas"] = 0 if name == "platform-mock" else 1
            for container in item["spec"]["template"]["spec"]["containers"]:
                if container["image"].startswith("laughtale-chat:"):
                    container["image"] = "laughtale-chat:capacity-v1"
                if name == "platform-mock":
                    continue
                for variable in container.get("env", []):
                    if variable["name"] == "EXTERNAL_ENABLED":
                        variable["value"] = "false"
                container["resources"]["limits"] = {
                    "cpu": "1000m" if name == "chat" else "500m",
                    "memory": "384Mi" if name == "chat" else "256Mi",
                }
    return items


def render():
    source = ROOT / "infra/k8s/chat-external"
    body = run(
        ["kubectl", "kustomize", str(source)], text=True, capture_output=True
    ).stdout
    for filename in ("relay.yaml", "distributed.yaml", "growth-proxy.yaml"):
        body += "\n---\n" + (source / filename).read_text()
    output = run(
        KUBE
        + [
            "create",
            "--dry-run=client",
            "--validate=false",
            "-f",
            "-",
            "-o",
            "json",
        ],
        input=body,
        text=True,
        capture_output=True,
    ).stdout
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": adapt(decode_documents(output)),
    }


def decode_documents(output):
    result = []
    decoder = json.JSONDecoder()
    while output.strip():
        value, end = decoder.raw_decode(output.lstrip())
        if value.get("kind") == "List":
            result.extend(value["items"])
        else:
            result.append(value)
        output = output.lstrip()[end:]
    return result


def credentials():
    path = PRIVATE / "app.json"
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(
                {name: secrets.token_hex(32) for name in ("ingress", "delivery")},
                stream,
            )
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise RuntimeError("Private application credential file required")
    tokens = json.loads(path.read_text())
    db = dict(line.split("=", 1) for line in ENV_FILE.read_text().splitlines())
    return {
        "chat-lab-env": {
            "DB_PRIMARY_URL": f"postgresql+asyncpg://chat_writer:{db['CHAT_WRITER_PASSWORD']}@chat-primary:5432/laughtale_chat"
        },
        "chat-growth-env": {"LAB_INGRESS_TOKEN": tokens["ingress"]},
        "chat-realtime-env": {
            "GATEWAY_DELIVERY_TOKEN": tokens["delivery"],
            "REDIS_URL": f"redis://default:{db['REDIS_PASSWORD']}@redis:6379/0",
        },
    }


def deploy():
    manifest = render()
    namespace = next(item for item in manifest["items"] if item["kind"] == "Namespace")
    run(
        KUBE + ["apply", "-f", "-"],
        input=json.dumps(namespace),
        text=True,
        capture_output=True,
    )
    for name, values in credentials().items():
        existing = run(
            KUBE
            + [
                "-n",
                NAMESPACE,
                "get",
                "secret",
                name,
                "--ignore-not-found",
                "-o",
                "name",
            ],
            text=True,
            capture_output=True,
        ).stdout.strip()
        if not existing:
            item = {
                "apiVersion": "v1",
                "kind": "Secret",
                "type": "Opaque",
                "immutable": True,
                "metadata": {"name": name, "namespace": NAMESPACE},
                "stringData": values,
            }
            run(
                KUBE + ["create", "-f", "-"],
                input=json.dumps(item),
                text=True,
                capture_output=True,
            )
    config = run(
        KUBE
        + [
            "-n",
            NAMESPACE,
            "create",
            "configmap",
            "chat-growth-proxy-source",
            "--from-file=" + str(ROOT / "infra/k8s/chat-external/lab_proxy.py"),
            "--dry-run=client",
            "-o",
            "json",
        ],
        text=True,
        capture_output=True,
    ).stdout
    run(KUBE + ["apply", "-f", "-"], input=config, text=True, capture_output=True)
    run(
        KUBE + ["apply", "-f", "-"],
        input=json.dumps(manifest),
        text=True,
        capture_output=True,
    )
    print(
        "Capacity deployments applied; readiness and data flow still require verification."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["bootstrap", "render", "deploy"])
    args = parser.parse_args()
    guarded_engine()
    if args.action == "bootstrap":
        bootstrap()
    elif args.action == "render":
        guarded_cluster()
        print(json.dumps(render(), indent=2))
    else:
        guarded_cluster()
        deploy()


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(
            f"Capacity operation failed (exit {error.returncode}); output suppressed to protect credentials"
        ) from None
