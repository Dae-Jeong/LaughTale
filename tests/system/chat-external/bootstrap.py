"""Prepare ignored, private local configuration; never start or change services."""

import argparse
import json
import os
import secrets
import subprocess
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from oracle import PROFILES, EvidenceError, identifier


def prepare(repo: Path, run_id: str) -> Path:
    identifier(run_id)
    destination = repo / ".artifacts" / "chat-external" / run_id
    for path in (repo / ".artifacts", destination.parent, destination):
        if path.is_symlink():
            raise EvidenceError("symlink_artifact_path")
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "--", str(destination / "chat.env")],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if ignored.returncode != 0:
        raise EvidenceError("artifact_path_must_be_ignored")
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    ids = {
        profile: str(
            uuid5(NAMESPACE_URL, f"laughtale:external-smoke:{run_id}:{profile}")
        )
        for profile in sorted(PROFILES)
    }
    chat_control, mock_control, service = (secrets.token_urlsafe(32) for _ in range(3))
    connection_tokens = {
        connection: secrets.token_urlsafe(32) for connection in ids.values()
    }
    settings = {
        "chat.env": {
            "EXTERNAL_ENABLED": "true",
            "DEV_SESSIONS_ENABLED": "true",
            "EXTERNAL_CONTROL_TOKEN": chat_control,
            "MOCK_API_URL": "http://127.0.0.1:18087",
            "MOCK_API_TOKEN": service,
            "EXTERNAL_CONNECTION_CREDENTIALS": json.dumps(
                {
                    connection: {
                        "profile": profile,
                        "token": connection_tokens[connection],
                    }
                    for profile, connection in ids.items()
                }
            ),
        },
        "mock.env": {
            "MOCK_SERVER_PORT": "18087",
            "MOCK_CHAT_BASE_URL": "http://127.0.0.1:18082",
            "MOCK_CONTROL_TOKEN": mock_control,
            "MOCK_SERVICE_TOKEN": service,
            "MOCK_CONNECTION_TOKENS": json.dumps(connection_tokens),
        },
        "harness.env": {
            "CHAT_TEST_RUN_ID": run_id,
            "CHAT_TEST_CONNECTION_IDS": json.dumps(ids),
            "CHAT_CONTROL_TOKEN": chat_control,
            "MOCK_CONTROL_TOKEN": mock_control,
            "CHAT_BASE_URL": "http://127.0.0.1:18082",
            "MOCK_BASE_URL": "http://127.0.0.1:18087",
            "CHAT_TEST_ORIGIN": "http://127.0.0.1:18083",
        },
    }
    for filename, values in settings.items():
        descriptor = os.open(
            destination / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            for name, value in values.items():
                output.write(f"{name}='{value}'\n")
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="Create private configuration files, without printing their contents",
    )
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    if not args.prepare:
        parser.error("--prepare is required")
    try:
        prepare(Path(__file__).resolve().parents[3], args.run_id)
    except (EvidenceError, OSError, ValueError):
        parser.exit(
            2,
            "Configuration preparation refused; check ignore rules, run ID and existing artifacts.\n",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
