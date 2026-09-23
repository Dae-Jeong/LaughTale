"""Capacity lab only: explicit engine, private generated credentials, no deletion."""

import argparse
import json
import os
import secrets
import subprocess
from pathlib import Path

from capacity import EXTERNAL, EXTERNAL_ID, IMAGE_ID, MOUNT, ROOT, check_volume, info

CONTEXT = "lima-laughtale-capacity"
SOCKET = "unix:///Volumes/LaughtaleLab/lima/laughtale-capacity/sock/docker.sock"
PRIVATE = ROOT / ".artifacts/chat-capacity/secrets"
ENV_FILE = PRIVATE / "infra.env"
KEYS = (
    "POSTGRES_PASSWORD",
    "CHAT_WRITER_PASSWORD",
    "CHAT_READER_PASSWORD",
    "REPLICATION_PASSWORD",
    "REDIS_PASSWORD",
)


def run(argv, **kwargs):
    return subprocess.run(argv, check=True, **kwargs)


def guarded_engine():
    check_volume(info(EXTERNAL), EXTERNAL_ID, EXTERNAL)
    check_volume(info(MOUNT), IMAGE_ID, MOUNT)
    value = json.loads(
        run(
            ["docker", "context", "inspect", CONTEXT],
            capture_output=True,
            text=True,
        ).stdout
    )[0]
    if value["Endpoints"]["docker"]["Host"] != SOCKET:
        raise RuntimeError("Unexpected capacity engine endpoint")
    name = run(
        ["docker", "--context", CONTEXT, "info", "--format", "{{.Name}}"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if name != "lima-laughtale-capacity":
        raise RuntimeError("Unexpected capacity engine identity")


def prepare():
    if any(path.is_symlink() for path in (PRIVATE, *PRIVATE.parents, ENV_FILE)):
        raise RuntimeError("Symlink not allowed for private artifacts")
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(PRIVATE, 0o700)
    if not ENV_FILE.exists():
        descriptor = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            for key in KEYS:
                stream.write(f"{key}={secrets.token_hex(32)}\n")
    if ENV_FILE.stat().st_mode & 0o077:
        raise RuntimeError("Private env permissions must be 0600")
    run(["git", "check-ignore", "-q", str(ENV_FILE)], cwd=ROOT)


def save_kubeconfig():
    env = dict(os.environ, DOCKER_HOST=SOCKET)
    env.pop("DOCKER_CONTEXT", None)
    result = run(
        ["k3d", "kubeconfig", "get", "laughtale-capacity"],
        env=env,
        capture_output=True,
        text=True,
    )
    path = PRIVATE / "kubeconfig.yaml"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(result.stdout)
    print("Private capacity kubeconfig saved; default kubeconfig unchanged.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["prepare", "up", "status", "cluster", "kubeconfig"]
    )
    args = parser.parse_args()
    guarded_engine()
    prepare()
    compose = [
        "docker",
        "--context",
        CONTEXT,
        "compose",
        "--env-file",
        str(ENV_FILE),
        "-f",
        str(ROOT / "infra/lima/capacity.compose.yaml"),
    ]
    if args.action == "prepare":
        run(compose + ["config", "--quiet"])
        print("Capacity configuration validated; private credentials retained.")
    elif args.action == "up":
        run(
            [
                "docker",
                "--context",
                CONTEXT,
                "build",
                "-f",
                str(ROOT / "infra/lima/Postgres.Dockerfile"),
                "-t",
                "laughtale-capacity-postgres:v2",
                str(ROOT),
            ]
        )
        run(compose + ["up", "-d", "--wait", "--wait-timeout", "180"])
    elif args.action == "status":
        run(compose + ["ps"])
    elif args.action == "kubeconfig":
        save_kubeconfig()
    else:
        env = dict(os.environ, DOCKER_HOST=SOCKET)
        env.pop("DOCKER_CONTEXT", None)
        lima_env = dict(os.environ, LIMA_HOME=str(MOUNT / "lima"))
        fake_dir = str(Path.home() / ".config/k3d/.k3d-laughtale-capacity-server-0")
        shell = ["limactl", "shell", "--workdir", "/", "laughtale-capacity"]
        run(shell + ["sudo", "mkdir", "-p", fake_dir], env=lima_env)
        run(
            [
                "limactl",
                "copy",
                str(ROOT / "infra/lima/k3d-meminfo"),
                "laughtale-capacity:/tmp/laughtale-capacity-meminfo",
            ],
            env=lima_env,
        )
        run(
            shell
            + [
                "sudo",
                "install",
                "-m",
                "644",
                "/tmp/laughtale-capacity-meminfo",
                fake_dir + "/meminfo",
            ],
            env=lima_env,
        )
        # No default kubeconfig mutation; no automatic rollback of partial state.
        run(
            [
                "k3d",
                "cluster",
                "create",
                "--config",
                str(ROOT / "infra/lima/capacity-cluster.yaml"),
                "--servers-memory",
                "4g",
                "--no-lb",
            ],
            env=env,
        )
        save_kubeconfig()


if __name__ == "__main__":
    main()
