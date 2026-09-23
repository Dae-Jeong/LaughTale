import asyncio
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx2
import pytest
from redis.asyncio import Redis
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from chat_service.adapters.gateway_delivery import GatewayDeliveryClient
from chat_service.adapters.redis_registry import SYNC, RedisRegistry
from chat_service.contracts.subscriptions import GatewayTarget, RegistryUnavailable
from chat_service.core.chat_hub import Peer
from chat_service.core.gateway_hub import GatewayHub
from chat_service.core.metrics import create_metrics
from chat_service.core.peer import CloseReason, OfferResult
from chat_service.core.realtime_settings import RealtimeSettings, validate_ip
from chat_service.http.gateway_security import GatewaySecurity
from chat_service.schemas.chat import MessageCreated, MessageData
from chat_service.schemas.responses import ErrorCode, Problem


def event():
    identity = uuid4()
    return MessageCreated(
        event_id=identity,
        message=MessageData(
            message_id=identity,
            conversation_id=uuid4(),
            sender_id=uuid4(),
            client_message_id=uuid4(),
            seq="1",
            text="synthetic",
            created_at=datetime.now(UTC),
        ),
    )


class Registry:
    def __init__(self):
        self.rooms = set()
        self.calls = []
        self.fail = False

    async def sync(self, target, rooms):
        self.calls.append(set(rooms))
        await asyncio.sleep(0)
        if self.fail:
            raise RuntimeError("test")
        self.rooms = set(rooms)

    async def remove(self, target):
        self.rooms.clear()

    async def targets(self, room):
        return []


def test_peer_dedup_payload_conflict_and_bounded_history():
    async def scenario():
        peer, created = Peer(), event()
        assert peer.offer(created) is OfferResult.ACCEPTED
        assert peer.offer(created) is OfferResult.DUPLICATE
        assert peer.queued_frames == 1
        changed = created.model_copy(
            update={"message": created.message.model_copy(update={"text": "different"})}
        )
        assert peer.offer(changed) is OfferResult.REJECTED
        assert peer.close_reason is CloseReason.EVENT_CONFLICT
        peer = Peer()
        for _ in range(300):
            assert peer.offer(event()) is OfferResult.ACCEPTED
            await peer.next_frame()
        assert peer.seen_count == 256

    asyncio.run(scenario())


def test_registry_registration_rollback_serialization_and_target_limit():
    async def scenario():
        registry = Registry()
        hub = GatewayHub(
            create_metrics(), registry, GatewayTarget(uuid4(), "10.42.0.10")
        )
        hub.last_refresh = time.monotonic()
        peer, room = Peer(), uuid4()
        hub.join(peer)
        await hub.subscribe(peer, room)
        assert room in registry.rooms
        await asyncio.gather(hub.unsubscribe(peer, room), hub.subscribe(peer, room))
        assert room in registry.rooms and room in peer.rooms
        registry.fail = True
        other = uuid4()
        with pytest.raises(RuntimeError):
            await hub.subscribe(peer, other)
        assert other not in peer.rooms
        hub.last_refresh -= 20
        with pytest.raises(RegistryUnavailable):
            await hub.subscribe(peer, uuid4())

    asyncio.run(scenario())


def test_queue_overflow_ack_requires_closed_socket():
    async def scenario():
        hub = GatewayHub(
            create_metrics(), Registry(), GatewayTarget(uuid4(), "10.42.0.10")
        )
        message = event()
        peer = Peer(rooms={message.message.conversation_id})
        hub.join(peer)
        for _ in range(64):
            assert peer.offer(event()) is OfferResult.ACCEPTED
        task = asyncio.create_task(hub.deliver(message))
        await asyncio.sleep(0)
        assert not task.done()
        assert peer.close_reason is CloseReason.QUEUE_OVERFLOW
        peer.mark_closed()
        assert await task == "resync_required"
        hub.leave(peer)
        assert await hub.deliver(message) == "no_subscribers"

    asyncio.run(scenario())


def test_expiry_watchdog_preserves_confirmed_auth_revocation():
    async def scenario():
        hub = GatewayHub(
            create_metrics(), Registry(), GatewayTarget(uuid4(), "10.42.0.10")
        )
        hub.last_refresh = time.monotonic()
        revoked = Peer()
        revoked.request_close(CloseReason.AUTH_REVOKED)
        expired = Peer(session_valid_until=time.monotonic() - 1)
        hub.join(revoked)
        hub.join(expired)
        task = asyncio.create_task(hub.watch_expiry())
        try:
            await asyncio.sleep(0.25)
            assert revoked.close_reason is CloseReason.AUTH_REVOKED
            assert expired.close_reason is CloseReason.SESSION_EXPIRED
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_ssrf_and_internal_token_boundary():
    for address in (
        "169.254.169.254",
        "10.42.1.2",
        "localhost",
        "127.0.0.1",
        "10.42.0.1:80",
    ):
        with pytest.raises(ValueError):
            validate_ip(address)
    assert validate_ip("10.42.0.12") == "10.42.0.12"
    settings = RealtimeSettings(
        _env_file=None,
        app_environment="isolated-lab",
        network_profile="isolated-lab",
        redis_url="redis://default:test-secret@redis:6379/0",
        gateway_delivery_token="test-secret-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        gateway_ip="10.42.0.12",
    )

    async def ok(request):
        return JSONResponse({"ok": True})

    app = GatewaySecurity(
        Starlette(routes=[Route("/internal/events", ok, methods=["POST"])]), settings
    )

    async def scenario():
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app, client=("10.42.0.11", 1234)),
            base_url="http://10.42.0.12:18082",
        ) as client:
            assert (await client.post("/internal/events", json={})).status_code == 403
            headers = {
                "Authorization": "Bearer "
                + settings.gateway_delivery_token.get_secret_value()
            }
            assert (
                await client.post("/internal/events", json={}, headers=headers)
            ).status_code == 200
            assert (
                await client.post(
                    "/internal/events",
                    json={},
                    headers={**headers, "Forwarded": "for=x"},
                )
            ).status_code == 403
            assert (
                await client.post(
                    "/internal/events", content=b"x" * 16385, headers=headers
                )
            ).status_code == 413

    asyncio.run(scenario())


def test_delivery_rejects_wrong_instance_and_no_remote_redirect():
    async def scenario():
        message = event()
        target = GatewayTarget(uuid4(), "10.42.0.10")

        async def response(request):
            return httpx2.Response(
                200,
                json={
                    "data": {
                        "instance_id": str(uuid4()),
                        "event_id": str(message.event_id),
                        "outcome": "accepted",
                    }
                },
            )

        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(response), follow_redirects=False
        ) as client:
            delivery = GatewayDeliveryClient(client, "test")
            with pytest.raises(ValueError, match="IDENTITY"):
                await delivery.send(target, message)
            with pytest.raises(ValueError):
                await delivery.send(GatewayTarget(uuid4(), "169.254.169.254"), message)

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [403, 409, 413, 422, 503])
def test_delivery_client_rejects_problem_without_parsing_as_success(status):
    async def scenario():
        problem = Problem(
            title="Synthetic failure",
            status=status,
            code=ErrorCode.HTTP_ERROR,
            request_id=uuid4().hex,
        )
        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda request: httpx2.Response(
                    status,
                    json=problem.model_dump(mode="json"),
                    headers={"content-type": "application/problem+json"},
                )
            )
        ) as client:
            delivery = GatewayDeliveryClient(client, "synthetic")
            with pytest.raises(ValueError, match="^GATEWAY_NOT_ACCEPTED$"):
                await delivery.send(GatewayTarget(uuid4(), "10.42.0.10"), event())

    asyncio.run(scenario())


def test_redis_script_uses_server_time_and_bounds_untrusted_targets():
    async def scenario():
        redis = AsyncMock(spec=Redis)
        redis.eval = AsyncMock(return_value=[str(uuid4()), "10.42.0.10"] * 17)
        with pytest.raises(RegistryUnavailable):
            await RedisRegistry(redis).targets(uuid4())

    assert "redis.call('TIME')" in SYNC and "30000" in SYNC
    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("host", "origin", "allowed"),
    [
        ("127.0.0.1:18085", "http://127.0.0.1:18083", True),
        ("127.0.0.1:18082", "http://127.0.0.1:18083", False),
        ("localhost:18085", "http://127.0.0.1:18083", False),
        ("127.0.0.1:18085", "http://evil.test", False),
    ],
)
def test_separate_gateway_browser_port_keeps_exact_host_and_origin(
    host, origin, allowed
):
    async def scenario():
        reached = False
        sent = []

        async def app(scope, receive, send):
            nonlocal reached
            reached = True

        settings = RealtimeSettings(
            app_environment="isolated-lab",
            network_profile="isolated-lab",
            redis_url="redis://test:synthetic@redis:6379/0",
            gateway_delivery_token="synthetic-gateway-secret-for-test-123456",
            gateway_browser_port=18085,
        )
        await GatewaySecurity(app, settings)(
            {
                "type": "websocket",
                "path": "/v1/ws",
                "client": ("127.0.0.1", 12345),
                "headers": [(b"host", host.encode()), (b"origin", origin.encode())],
            },
            AsyncMock(),
            AsyncMock(side_effect=sent.append),
        )
        assert reached is allowed
        if not allowed:
            assert sent == [{"type": "websocket.close", "code": 1008}]

    asyncio.run(scenario())
