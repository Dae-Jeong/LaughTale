import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

SERVICE_ROOT = Path(__file__).resolve().parents[1]


def test_revision_filenames_follow_numbered_history() -> None:
    scripts = ScriptDirectory.from_config(Config(str(SERVICE_ROOT / "alembic.ini")))
    assert len(scripts.get_heads()) == 1
    for revision in scripts.walk_revisions():
        assert re.fullmatch(r"[0-9]{4}", revision.revision)
        assert Path(revision.path).stem.startswith(revision.revision + "_")
        if revision.down_revision is None:
            assert revision.revision == "0001"
        else:
            assert isinstance(revision.down_revision, str)
            assert int(revision.revision) == int(revision.down_revision) + 1


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
    assert "CREATE TABLE chat.message_outbox" in result.stdout
