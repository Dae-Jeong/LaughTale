import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect

from chat_service.contracts.chat import Actor
from chat_service.core.chat_hub import SESSION_LEASE_SECONDS, ChatHub, Peer
from chat_service.core.metrics import create_metrics
from chat_service.core.peer import CloseReason
from chat_service.core.sessions import LocalSessions, LocalSessionStore
from chat_service.dependencies.connections import get_chat_connection
from chat_service.schemas.chat import MessageCreated, MessageData
from chat_service.services.chat import ChatService
from chat_service.transports.chat_ws import ChatConnection
from tests.test_chat_hub import CountingService


class Socket:
    def __init__(self):
        self.sent = []

    async def send_text(self, frame):
        self.sent.append(frame)


def queued_peer(actor, room, token):
    peer = Peer(actor_id=actor, rooms={room}, token=token)
    identity = uuid4()
    peer.offer(
        MessageCreated(
            event_id=identity,
            message=MessageData(
                message_id=identity,
                conversation_id=room,
                sender_id=actor,
                client_message_id=uuid4(),
                seq="1",
                text="synthetic queued",
                created_at=datetime.now(UTC),
            ),
        )
    )
    return peer


def test_confirmed_membership_revocation_never_sends_queued_body():
    async def scenario():
        actor, room = uuid4(), uuid4()
        sessions = LocalSessionStore(LocalSessions(lambda: datetime.now(UTC)))
        token = await sessions.issue(Actor(actor, "synthetic"))
        peer = queued_peer(actor, room, token)
        hub = ChatHub(create_metrics())
        hub.join(peer)
        # Primary에 방 회원권이 없는 응답입니다.
        assert await hub.reconcile(CountingService(uuid4(), room), sessions)
        assert peer.close_reason is CloseReason.AUTH_REVOKED
        socket = Socket()
        connection = ChatConnection(sessions, lambda: cast(ChatService, None), hub)
        connection.socket, connection.peer = cast(WebSocket, socket), peer
        await connection.send()
        assert socket.sent == []

    asyncio.run(scenario())


def test_revocation_during_session_refresh_does_not_release_dequeued_frame():
    async def scenario():
        actor, room = uuid4(), uuid4()
        peer = queued_peer(actor, room, "synthetic")
        peer.session_valid_until = time.monotonic() - 1

        class Sessions(LocalSessionStore):
            async def resolve(self, token):
                peer.request_close(CloseReason.AUTH_REVOKED)
                return Actor(actor, "synthetic")

        socket = Socket()
        connection = ChatConnection(
            Sessions(LocalSessions(lambda: datetime.now(UTC))),
            lambda: cast(ChatService, None),
            ChatHub(create_metrics()),
        )
        connection.socket, connection.peer = cast(WebSocket, socket), peer
        await connection.send()
        assert socket.sent == []

    asyncio.run(scenario())


def test_transport_disconnect_attempts_retryable_close_code():
    class DisconnectingSocket:
        def __init__(self, actor):
            self.cookies = {"linky_session": "synthetic"}
            self.closed_with = None

            class Sessions:
                async def resolve(self, token):
                    return Actor(actor, "synthetic")

            self.app = SimpleNamespace(
                state=SimpleNamespace(
                    sessions=Sessions(),
                    primary_session_factory=object(),
                    database_metrics=object(),
                    chat_hub=ChatHub(create_metrics()),
                )
            )

        async def accept(self):
            return None

        async def receive(self):
            raise WebSocketDisconnect

        async def send_text(self, frame):
            raise AssertionError("no frame should be sent")

        async def close(self, *, code):
            self.closed_with = code

    async def scenario():
        socket = DisconnectingSocket(uuid4())
        await get_chat_connection(cast(WebSocket, socket)).run(cast(WebSocket, socket))
        assert socket.closed_with == 1013

    asyncio.run(scenario())


def test_auth_lease_starts_before_primary_query_and_stale_success_cannot_extend_it():
    async def scenario():
        actor = Actor(uuid4(), "synthetic")
        now = 100.0

        class Sessions(LocalSessionStore):
            async def resolve_many(self, tokens):
                nonlocal now
                now += SESSION_LEASE_SECONDS + 0.1
                return {"synthetic": actor}

        peer = Peer(actor_id=actor.user_id, token="synthetic")
        hub = ChatHub(create_metrics())
        hub.join(peer)
        with patch("chat_service.core.chat_hub.time.monotonic", lambda: now):
            assert await hub.reconcile(
                CountingService(actor.user_id, uuid4()),
                Sessions(LocalSessions(lambda: datetime.now(UTC))),
            )
        assert peer.session_valid_until == 100.0 + SESSION_LEASE_SECONDS
        assert peer.close_reason is CloseReason.SESSION_EXPIRED
        assert peer.queued_frames == 0

    asyncio.run(scenario())


def test_sender_discards_stale_primary_success_before_watchdog_runs():
    async def scenario():
        actor, room = uuid4(), uuid4()
        peer = queued_peer(actor, room, "synthetic")
        now = 100.0

        class Sessions(LocalSessionStore):
            async def resolve(self, token):
                nonlocal now
                now += SESSION_LEASE_SECONDS + 0.1
                return Actor(actor, "synthetic")

        socket = Socket()
        connection = ChatConnection(
            Sessions(LocalSessions(lambda: datetime.now(UTC))),
            lambda: cast(ChatService, None),
            ChatHub(create_metrics()),
        )
        connection.socket, connection.peer = cast(WebSocket, socket), peer
        with patch("chat_service.transports.chat_ws.time.monotonic", lambda: now):
            await connection.send()
        assert peer.close_reason is CloseReason.SESSION_EXPIRED
        assert socket.sent == []

    asyncio.run(scenario())
