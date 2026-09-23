import asyncio

import pytest
from starlette.types import Message

from chat_service.http.admission import MessageAdmission


@pytest.mark.parametrize("failure", [None, RuntimeError, asyncio.CancelledError])
def test_capacity_rejection_and_release(failure):
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def app(scope, receive, send):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            if failure:
                raise failure()

        gate = MessageAdmission(app, limit=1)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/internal-conversations/synthetic/messages",
            "headers": [],
        }
        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        running = asyncio.create_task(gate(scope, receive, send))
        await entered.wait()
        await gate(scope, receive, send)
        assert calls == 1 and sent[0]["status"] == 503
        assert (b"x-lab-admission", b"rejected") in sent[0]["headers"]
        release.set()
        if failure:
            with pytest.raises(failure):
                await running
        else:
            await running
        assert gate.active == 0

    asyncio.run(scenario())


def test_read_requests_bypass_message_capacity():
    async def scenario():
        calls = []

        async def app(scope, incoming, outgoing):
            assert incoming is receive and outgoing is send
            calls.append(scope["path"])

        async def receive() -> Message:
            raise AssertionError("Bypass must not consume the request body")

        async def send(message: Message) -> None:
            raise AssertionError("Bypass must not produce a response")

        gate = MessageAdmission(app, 1)
        gate.active = 1
        await gate({"type": "http", "method": "GET", "path": "/metrics"}, receive, send)
        assert calls == ["/metrics"] and gate.active == 1

    asyncio.run(scenario())
