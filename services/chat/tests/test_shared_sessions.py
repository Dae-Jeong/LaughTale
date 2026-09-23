import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from chat_service.contracts.chat import Actor
from chat_service.core.sessions import LocalSessions, LocalSessionStore
from chat_service.core.settings import Settings
from chat_service.core.shared_sessions import session_hash
from chat_service.exceptions.chat import UnauthenticatedError


def test_async_local_adapter_preserves_cookie_and_revocation():
    async def scenario():
        store = LocalSessionStore(LocalSessions(lambda: datetime.now(UTC)))
        actor = Actor(uuid4(), "synthetic")
        token = await store.issue(actor)
        assert len(token) == 43 and token not in repr(store)
        assert await store.resolve(token) == actor
        assert await store.resolve_many((token, "invalid")) == {token: actor}
        await store.revoke(token)
        with pytest.raises(UnauthenticatedError):
            await store.resolve(token)
        with pytest.raises(ValueError):
            await store.resolve_many(("invalid",) * 257)

    asyncio.run(scenario())


def test_hash_input_bounds():
    for value in (None, "", "x" * 42, "x" * 44, "한" * 43, " " * 43):
        assert session_hash(value) is None
    assert len(session_hash("x" * 43) or "") == 64


def test_postgres_sessions_require_explicit_lab_and_database():
    invalid: list[dict[str, Any]] = [
        {"session_backend": "postgres"},
        {
            "session_backend": "postgres",
            "db_primary_url": "postgresql+asyncpg://user:synthetic@127.0.0.1:5440/test",
        },
        {
            "session_backend": "postgres",
            "network_profile": "isolated-lab",
            "app_environment": "isolated-lab",
            "server_host": "0.0.0.0",
        },
    ]
    for values in invalid:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **values)
    assert Settings(_env_file=None).session_backend == "local"
