import os
import subprocess
import sys
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "url",
    [
        "",
        "invalid",
        "postgresql+asyncpg://chat_writer:synthetic-secret@127.0.0.1:5440/laughtale_chat",
        "postgresql+asyncpg://chat_migrator:synthetic-secret@127.0.0.1:5433/laughtale_chat",
    ],
)
def test_cli_refuses_missing_or_wrong_target(url: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(SERVICE_ROOT / "alembic.ini"),
            "upgrade",
            "head",
        ],
        env={**os.environ, "CHAT_MIGRATION_URL": url},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "synthetic-secret" not in result.stdout + result.stderr


def test_offline_sql_needs_no_credentials() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(SERVICE_ROOT / "alembic.ini"),
            "upgrade",
            "head",
            "--sql",
        ],
        env={**os.environ, "CHAT_MIGRATION_URL": ""},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "SET LOCAL ROLE chat_owner" in result.stdout
    assert "CREATE TABLE chat.messages" in result.stdout
    assert "uq_messages_idempotency" in result.stdout
    assert "outbox" not in result.stdout
