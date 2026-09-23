import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError
from starlette.testclient import TestClient

from chat_service.bootstrap.app import create_app
from chat_service.contracts.chat import Actor
from chat_service.core.chat_hub import ChatHub, Peer
from chat_service.core.metrics import create_metrics
from chat_service.core.peer import CloseReason, OfferResult
from chat_service.core.sessions import LocalSessions
from chat_service.core.settings import Settings
from chat_service.export_contracts import artifacts
from chat_service.schemas.chat import (
    MAX_SEQ,
    MessageSeq,
    Seq,
    Subscribed,
    WSError,
    seq_pattern,
)
from chat_service.schemas.responses import ErrorCode
from chat_service.transports.chat_ws import close_code_for


@pytest.mark.parametrize("value", ["0", "1", "9007199254740993", str(MAX_SEQ)])
def test_decimal_seq_contract(value: str) -> None:
    assert TypeAdapter(Seq).validate_python(value) == value
    assert re.fullmatch(seq_pattern(), value)


@pytest.mark.parametrize(
    "value", ["-1", "01", "1.0", "+1", str(MAX_SEQ + 1), "9" * 100, 1]
)
def test_invalid_seq(value: object) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(Seq).validate_python(value)
    if isinstance(value, str):
        assert not re.fullmatch(seq_pattern(), value)


def test_message_seq_starts_at_one() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(MessageSeq).validate_python("0")


def test_generated_contract_drift() -> None:
    root = Path(__file__).resolve().parents[3] / "contracts/chat"
    for name, expected in artifacts().items():
        assert json.loads((root / name).read_text()) == expected


def test_session_expiry_rotation_capacity_and_secret_repr() -> None:
    now = datetime(2026, 9, 8, tzinfo=UTC)
    sessions = LocalSessions(lambda: now, ttl_seconds=10, capacity=2)
    actor = Actor(uuid4(), "synthetic")
    first = sessions.issue(actor)
    second = sessions.issue(actor, first)
    assert first not in sessions.sessions and first not in repr(sessions)
    sessions.issue(actor)
    sessions.issue(actor)
    assert second not in sessions.sessions and len(sessions.sessions) == 2
    now += timedelta(seconds=10)
    with pytest.raises(Exception, match="^$"):
        sessions.resolve(next(iter(sessions.sessions)))


@pytest.mark.parametrize(
    "values",
    [
        {"app_environment": "production"},
        {"server_host": "0.0.0.0"},
        {"dev_origin": "http://evil.example"},
        {"dev_origin": "http://127.0.0.1:18083/"},
    ],
)
def test_unsafe_session_configuration(values: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"dev_sessions_enabled": True, **values})


def test_disabled_routes_are_not_mounted() -> None:
    app = create_app(Settings(app_environment="production"))
    assert "/v1/dev/session" not in app.openapi()["paths"]
    with TestClient(app) as client:
        assert (
            client.post("/v1/dev/session", json={"user": "user_a"}).status_code == 404
        )


@pytest.mark.parametrize(
    "headers,client_host",
    [
        ({}, "127.0.0.1"),
        ({"Origin": "http://evil.example"}, "127.0.0.1"),
        ({"Origin": "http://127.0.0.1:18083", "Host": "evil.example"}, "127.0.0.1"),
        (
            {"Origin": "http://127.0.0.1:18083", "X-Forwarded-For": "127.0.0.1"},
            "127.0.0.1",
        ),
        ({"Origin": "http://127.0.0.1:18083"}, "192.0.2.1"),
    ],
)
def test_csrf_host_proxy_and_peer_rejection(
    headers: dict[str, str], client_host: str
) -> None:
    app = create_app(Settings(dev_sessions_enabled=True))
    with TestClient(
        app, base_url="http://127.0.0.1:18082", client=(client_host, 1234)
    ) as client:
        result = client.post(
            "/v1/dev/session", json={"user": "user_a"}, headers=headers
        )
        assert result.status_code == 403
        assert result.headers["Cache-Control"] == "no-store"
        assert result.headers["X-Request-ID"] == result.json()["request_id"]


def test_queue_count_bytes_and_safe_metrics() -> None:
    async def scenario() -> None:
        peer = Peer()
        for _ in range(64):
            assert (
                peer.offer(WSError(code=ErrorCode.INVALID_INPUT))
                is OfferResult.ACCEPTED
            )
        assert peer.offer(WSError(code=ErrorCode.INVALID_INPUT)) is OfferResult.REJECTED
        assert peer.close_reason is CloseReason.QUEUE_OVERFLOW
        other = Peer()
        while (
            other.offer(Subscribed(conversation_id=uuid4(), head_seq="0"))
            is OfferResult.ACCEPTED
        ):
            pass
        assert other.close_reason is CloseReason.QUEUE_OVERFLOW
        hub = ChatHub(create_metrics())
        assert hub.join(peer)
        hub.leave(peer)
        hub.leave(peer)
        assert hub.metrics.registry.get_sample_value("chat_ws_connections") == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", list(CloseReason))
def test_close_reason_websocket_code_mapping(reason: CloseReason) -> None:
    expected = 1008 if reason is CloseReason.AUTH_REVOKED else 1013
    assert close_code_for(reason) == expected
