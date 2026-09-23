"""단일 프로세스 best-effort 전달입니다. 원본은 Primary history와 head입니다."""

import asyncio
import time
from collections.abc import Callable
from random import uniform
from uuid import UUID

from prometheus_client import Counter, Gauge

from chat_service.contracts.subscriptions import ReconcileOutcome, ResyncReason
from chat_service.core.metrics import HttpMetrics
from chat_service.core.peer import (
    CloseReason,
    OfferResult,
    Peer,
)
from chat_service.core.sessions import SessionStore
from chat_service.exceptions.chat import UnauthenticatedError
from chat_service.schemas.chat import (
    HeadItem,
    Heads,
    MessageCreated,
    ResyncRequired,
)
from chat_service.services.chat import ChatService

MAX_CONNECTIONS = 128
MAX_SUBSCRIPTIONS = 16
MAX_FRAME_BYTES = 16384
SEND_TIMEOUT_SECONDS = 5
HEAD_INTERVAL_SECONDS = 10
# 조회 지연과 종료 처리에 여유를 남기는 로컬 인증 lease입니다.
SESSION_LEASE_SECONDS = 3


class ChatHub:
    def __init__(self, metrics: HttpMetrics) -> None:
        self.peers: set[Peer] = set()
        self.metrics = metrics
        self.connections = Gauge(
            "chat_ws_connections",
            "Active local WebSocket connections.",
            registry=metrics.registry,
        )
        self.overflows = Counter(
            "chat_ws_queue_overflows_total",
            "Socket queues requiring reconnect recovery.",
            registry=metrics.registry,
        )
        self.reconciliations = Counter(
            "chat_ws_reconciliations_total",
            "Primary authoritative head batch outcomes.",
            ("outcome",),
            registry=metrics.registry,
        )

    def record(self, action: Callable[[], None]) -> None:
        # 관측 실패가 저장 결과나 연결 정리를 덮지 않게 합니다.
        try:
            action()
        except Exception:
            self.metrics.failed = True

    def join(self, peer: Peer) -> bool:
        if len(self.peers) >= MAX_CONNECTIONS:
            return False
        self.peers.add(peer)
        self.record(self.connections.inc)
        return True

    def leave(self, peer: Peer) -> None:
        if peer in self.peers:
            self.peers.remove(peer)
            self.record(self.connections.dec)

    async def subscribe(self, peer: Peer, room: UUID) -> None:
        peer.rooms.add(room)

    async def unsubscribe(self, peer: Peer, room: UUID) -> None:
        peer.rooms.discard(room)

    async def detach(self, peer: Peer) -> None:
        self.leave(peer)

    def request_close_all(self, reason: CloseReason) -> None:
        for peer in tuple(self.peers):
            peer.request_close(reason)

    def publish(self, event: MessageCreated) -> None:
        for peer in tuple(self.peers):
            if event.message.conversation_id in peer.rooms and not peer.is_closing:
                if peer.offer(event) is OfferResult.REJECTED:
                    self.record(self.overflows.inc)

    async def reconcile(self, service: ChatService, sessions: SessionStore) -> bool:
        active = []
        checked_at = time.monotonic()
        resolved = await sessions.resolve_many(
            tuple({peer.token for peer in self.peers if peer.token})
        )
        for peer in tuple(self.peers):
            try:
                actor = resolved.get(peer.token)
                if actor is None or actor.user_id != peer.actor_id:
                    raise UnauthenticatedError
                peer.session_valid_until = checked_at + SESSION_LEASE_SECONDS
                if time.monotonic() >= peer.session_valid_until:
                    peer.request_close(CloseReason.SESSION_EXPIRED)
            except UnauthenticatedError:
                peer.request_close(CloseReason.AUTH_REVOKED)
            else:
                if not peer.is_closing:
                    active.append(peer)
        subscriptions = {peer: set(peer.rooms) for peer in active}
        rooms = {room for subscribed in subscriptions.values() for room in subscribed}
        actors = {peer.actor_id for peer in active if peer.actor_id is not None}
        if not rooms:
            return True
        try:
            # Gateway 전체의 활성 방을 한 번에 조회합니다. peer별 DB polling이 아닙니다.
            heads = {}
            ordered_rooms = sorted(rooms)
            for offset in range(0, len(ordered_rooms), 100):
                heads.update(
                    await service.heads(
                        actors, set(ordered_rooms[offset : offset + 100])
                    )
                )
        except Exception:
            self.record(
                lambda: self.reconciliations.labels(ReconcileOutcome.FAILED.value).inc()
            )
            for peer in active:
                for room in tuple(peer.rooms):
                    if (
                        peer.offer(
                            ResyncRequired(
                                conversation_id=room,
                                reason=ResyncReason.RECOVERY_UNAVAILABLE,
                            )
                        )
                        is OfferResult.REJECTED
                    ):
                        break
            return False
        self.record(
            lambda: self.reconciliations.labels(ReconcileOutcome.SUCCEEDED.value).inc()
        )
        for peer in active:
            items = []
            for room in subscriptions[peer] & peer.rooms:
                head = heads.get((peer.actor_id, room))
                if head is None:
                    peer.rooms.discard(room)
                    # 방 제거만으로는 이미 대기 중인 본문 전송을 막지 못합니다.
                    # 권한 폐기를 확인한 연결 전체를 종료하여 queued/in-flight sender를 취소합니다.
                    peer.request_close(CloseReason.AUTH_REVOKED)
                else:
                    items.append(HeadItem(conversation_id=room, head_seq=str(head)))
            if items and not peer.is_closing:
                peer.offer(Heads(items=items))
        return True

    async def reconcile_loop(
        self, service: ChatService, sessions: SessionStore
    ) -> None:
        failures = 0
        while True:
            await asyncio.sleep(HEAD_INTERVAL_SECONDS * uniform(0.9, 1.1))
            if await self.reconcile(service, sessions):
                failures = 0
            else:
                failures += 1
                if failures >= 3:
                    self.request_close_all(CloseReason.RECOVERY_UNAVAILABLE)
