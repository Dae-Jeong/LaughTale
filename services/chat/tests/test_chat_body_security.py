import asyncio

import pytest

from chat_service.core.settings import Settings
from chat_service.http import local_security
from chat_service.http.local_security import LocalChatSecurity


def scope():
    return {
        "type": "http",
        "path": "/v1/dev/session",
        "method": "POST",
        "client": ("127.0.0.1", 1),
        "headers": [
            (b"host", b"127.0.0.1:18082"),
            (b"origin", b"http://127.0.0.1:18083"),
        ],
        "state": {},
    }


def test_buffer_replayed_once_then_disconnect_forwarded() -> None:
    async def scenario() -> None:
        messages = iter(
            [
                {"type": "http.request", "body": b"{}", "more_body": False},
                {"type": "http.disconnect"},
            ]
        )

        async def receive():
            return next(messages)

        async def app(scope, receive, send):
            assert (await receive())["body"] == b"{}"
            assert (await receive())["type"] == "http.disconnect"

        async def send(message):
            pass

        await LocalChatSecurity(app, Settings(dev_sessions_enabled=True))(
            scope(), receive, send
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mode,expected",
    [("oversize", 413), ("timeout", 408), ("disconnect", None), ("cancel", None)],
)
def test_body_failure_never_calls_business(
    mode: str, expected: int | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_security, "BODY_TIMEOUT_SECONDS", 0.01)

    async def scenario() -> None:
        sent = []

        async def receive():
            if mode == "oversize":
                return {"type": "http.request", "body": b"x" * 16385}
            if mode == "disconnect":
                return {"type": "http.disconnect"}
            if mode == "cancel":
                raise asyncio.CancelledError()
            await asyncio.Future()

        async def app(scope, receive, send):
            pytest.fail("Business must not run")

        async def send(message):
            sent.append(message)

        if mode == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await LocalChatSecurity(app, Settings(dev_sessions_enabled=True))(
                    scope(), receive, send
                )
        else:
            await LocalChatSecurity(app, Settings(dev_sessions_enabled=True))(
                scope(), receive, send
            )
        if expected is None:
            assert not sent
        else:
            assert sent[0]["status"] == expected

    asyncio.run(scenario())
