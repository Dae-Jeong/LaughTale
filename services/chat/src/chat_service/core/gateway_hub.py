"""로컬 peer와 Redis 등록의 수명·실패 경계를 함께 관리합니다."""

import asyncio
import time
from uuid import UUID

from chat_service.contracts.subscriptions import (
    GatewayOutcome,
    GatewayTarget,
    RegistryUnavailable,
    SubscriptionRegistry,
)
from chat_service.core.chat_hub import ChatHub, Peer
from chat_service.core.metrics import HttpMetrics
from chat_service.core.peer import CloseReason, OfferResult
from chat_service.core.sessions import SessionStore
from chat_service.schemas.chat import MessageCreated
from chat_service.services.chat import ChatService


class GatewayHub(ChatHub):
    def __init__(
        self,
        metrics: HttpMetrics,
        registry: SubscriptionRegistry,
        target: GatewayTarget,
    ) -> None:
        super().__init__(metrics)
        self.registry, self.target = registry, target
        self.routing_lock = asyncio.Lock()
        self.last_refresh = 0.0
        self.draining = False

    def rooms(self) -> set[UUID]:
        return {
            room for peer in self.peers if not peer.is_closing for room in peer.rooms
        }

    def healthy(self) -> bool:
        return not self.draining and time.monotonic() - self.last_refresh < 15

    async def refresh_locked(self) -> None:
        async with asyncio.timeout(1):
            await self.registry.sync(self.target, self.rooms())
        self.last_refresh = time.monotonic()

    async def subscribe(self, peer: Peer, room: UUID) -> None:
        async with self.routing_lock:
            if not self.healthy() or len(self.rooms() | {room}) > 256:
                raise RegistryUnavailable("REGISTRY_UNAVAILABLE")
            peer.rooms.add(room)
            try:
                await self.refresh_locked()
            except BaseException:
                peer.rooms.discard(room)
                raise

    async def unsubscribe(self, peer: Peer, room: UUID) -> None:
        async with self.routing_lock:
            peer.rooms.discard(room)
            try:
                await self.refresh_locked()
            except Exception:
                # local 관심은 이미 제거됐습니다. TTL을 메시지 수신 보장으로 사용하지 않습니다.
                pass

    async def detach(self, peer: Peer) -> None:
        async with self.routing_lock:
            self.leave(peer)
            try:
                await self.refresh_locked()
            except Exception:
                pass

    async def deliver(self, event: MessageCreated) -> GatewayOutcome:
        selected = [
            peer for peer in self.peers if event.message.conversation_id in peer.rooms
        ]
        if not selected:
            return GatewayOutcome.NO_SUBSCRIBERS
        closing = []
        for peer in selected:
            if peer.is_closing or peer.offer(event) is OfferResult.REJECTED:
                closing.append(peer)
        if closing:
            # 단순 플래그 설정은 resync ACK가 아닙니다. 실제 socket close 완료를 기다립니다.
            async with asyncio.timeout(0.8):
                await asyncio.gather(*(peer.wait_closed() for peer in closing))
            return GatewayOutcome.RESYNC_REQUIRED
        return GatewayOutcome.ACCEPTED

    async def maintain(self, service: ChatService, sessions: SessionStore) -> None:
        while True:
            started = time.monotonic()
            try:
                async with self.routing_lock:
                    await self.refresh_locked()
            except Exception:
                pass
            if not self.healthy():
                self.request_close_all(CloseReason.REGISTRY_UNAVAILABLE)
            try:
                async with asyncio.timeout(1):
                    success = await self.reconcile(service, sessions)
                if not success:
                    self.request_close_all(CloseReason.RECOVERY_UNAVAILABLE)
            except Exception:
                self.request_close_all(CloseReason.RECOVERY_UNAVAILABLE)
            await asyncio.sleep(max(0, 2 - (time.monotonic() - started)))

    async def watch_expiry(self) -> None:
        while True:
            await asyncio.sleep(0.2)
            for peer in self.peers:
                if not peer.is_closing and not self.healthy():
                    peer.request_close(CloseReason.REGISTRY_UNAVAILABLE)
                elif (
                    not peer.is_closing and time.monotonic() >= peer.session_valid_until
                ):
                    peer.request_close(CloseReason.SESSION_EXPIRED)

    async def shutdown(self) -> None:
        self.draining = True
        self.request_close_all(CloseReason.SERVER_SHUTDOWN)
        async with self.routing_lock:
            async with asyncio.timeout(1):
                await self.registry.remove(self.target)
