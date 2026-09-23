import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from chat_service.contracts.chat import Actor
from chat_service.core.chat_hub import ChatHub, Peer
from chat_service.core.metrics import create_metrics
from chat_service.core.peer import CloseReason
from chat_service.core.sessions import LocalSessions, LocalSessionStore
from chat_service.services.chat import ChatService


class CountingService(ChatService):
    """Primary batch 경계의 조회 횟수·권한 결과를 제어하는 대역입니다."""

    def __init__(self, actor: UUID, room: UUID) -> None:
        self.actor_id = actor
        self.room = room
        self.calls = 0
        self.requested: set[UUID] = set()
        self.fail = False

    async def heads(
        self, actors: set[UUID], rooms: set[UUID]
    ) -> dict[tuple[UUID, UUID], int]:
        self.calls += 1
        self.requested = rooms
        if self.fail:
            raise RuntimeError("synthetic database failure")
        return {(self.actor_id, self.room): 9007199254740993}


def test_shared_head_batch_membership_expiry_and_failure() -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 8, tzinfo=UTC)
        sessions = LocalSessions(lambda: now, ttl_seconds=5)
        actor, room, outsider = uuid4(), uuid4(), uuid4()
        service = CountingService(actor, room)
        hub = ChatHub(create_metrics())
        peers = [
            Peer(
                actor_id=identity,
                token=sessions.issue(Actor(identity, "test")),
                rooms={room},
            )
            for identity in (actor, actor, outsider)
        ]
        for peer in peers:
            hub.join(peer)
        assert await hub.reconcile(service, LocalSessionStore(sessions))
        assert service.calls == 1 and service.requested == {room}
        for peer in peers[:2]:
            assert json.loads(await peer.next_frame())["items"] == [
                {"conversation_id": str(room), "head_seq": "9007199254740993"}
            ]
        assert peers[2].close_reason is CloseReason.AUTH_REVOKED
        assert peers[2].queued_frames == 0
        assert not peers[2].rooms
        service.fail = True
        assert not await hub.reconcile(service, LocalSessionStore(sessions))
        assert json.loads(await peers[0].next_frame())["type"] == "resync_required"
        now += timedelta(seconds=5)
        assert await hub.reconcile(service, LocalSessionStore(sessions))
        assert service.calls == 2
        assert all(peer.close_reason is CloseReason.AUTH_REVOKED for peer in peers)

    asyncio.run(scenario())
