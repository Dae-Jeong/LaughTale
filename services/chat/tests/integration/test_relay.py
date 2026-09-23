import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from tests.integration.test_chat_service import SEED_ROOM, A, B, service_database

from chat_service.contracts.relay import RelayClaim
from chat_service.domain.chat import MessagePayload
from chat_service.models.chat import Conversation, Member, MessageOutbox
from chat_service.repositories.relay import RelayRepository
from chat_service.services.relay import OutboxRelay

pytestmark = pytest.mark.postgres


def test_four_relays_cannot_claim_same_room_tail(postgres_url: str) -> None:
    async def scenario():
        async with service_database(postgres_url) as service:
            first = await service.store(A, SEED_ROOM, uuid4(), MessagePayload("head"))
            await service.store(A, SEED_ROOM, uuid4(), MessagePayload("tail"))
            entered, finished = asyncio.Barrier(4), asyncio.Barrier(4)

            async def claim():
                async with service.factory() as session, session.begin():
                    await entered.wait()
                    result = await RelayRepository(session).claim()
                    await finished.wait()
                    return result

            async with asyncio.timeout(10):
                claims = await asyncio.gather(*(claim() for _ in range(4)))
            claimed = [c for c in claims if c is not None]
            assert len(claimed) == 1
            assert claimed[0].event_id == first.message.message_id
            await service.store(A, SEED_ROOM, uuid4(), MessagePayload("new tail"))
            async with service.factory() as session, session.begin():
                assert await RelayRepository(session).claim() is None

    asyncio.run(scenario())


def test_rolled_back_claim_keeps_same_head_available(postgres_url: str) -> None:
    async def scenario():
        async with service_database(postgres_url) as service:
            first = await service.store(A, SEED_ROOM, uuid4(), MessagePayload("head"))
            await service.store(A, SEED_ROOM, uuid4(), MessagePayload("tail"))
            async with service.factory() as session:
                await session.begin()
                aborted = await RelayRepository(session).claim()
                await session.rollback()
            async with service.factory() as session, session.begin():
                current = await RelayRepository(session).claim()
                assert current and aborted
                assert current.event_id == aborted.event_id == first.message.message_id
                assert current.attempt == 1 and current.token != aborted.token
                assert not await RelayRepository(session).finish(aborted)

    asyncio.run(scenario())


class Publisher:
    def __init__(self, error: BaseException | None = None):
        self.calls: list[RelayClaim] = []
        self.error = error

    async def publish(self, claim: RelayClaim) -> None:
        self.calls.append(claim)
        if self.error:
            raise self.error


def test_ack_marks_published_and_releases_capacity(
    postgres_url: str, monkeypatch
) -> None:
    monkeypatch.setattr("chat_service.repositories.chat.OUTBOX_ROW_LIMIT", 1)

    async def scenario():
        async with service_database(postgres_url) as service:
            first = await service.store(A, SEED_ROOM, uuid4(), MessagePayload("one"))
            publisher = Publisher()
            relay = OutboxRelay(service.factory, publisher)
            assert await relay.once() == "published"
            assert await relay.once() == "idle"
            assert publisher.calls[0].event_id == first.message.message_id
            await service.store(B, SEED_ROOM, uuid4(), MessagePayload("two"))
            assert await relay.once() == "published"
            async with service.factory() as session:
                rows = (await session.scalars(select(MessageOutbox))).all()
                assert len(rows) == 2
                assert all(row.published_at and row.claim_token is None for row in rows)

    asyncio.run(scenario())


def test_failure_delays_head_and_cancel_preserves_lease(postgres_url: str) -> None:
    async def scenario():
        async with service_database(postgres_url) as service:
            await service.store(A, SEED_ROOM, uuid4(), MessagePayload("one"))
            await service.store(A, SEED_ROOM, uuid4(), MessagePayload("two"))
            publisher = Publisher(RuntimeError("injected"))
            relay = OutboxRelay(service.factory, publisher)
            assert await relay.once() == "retry"
            assert await relay.once() == "idle"
            async with service.factory() as session, session.begin():
                row = await session.get(MessageOutbox, publisher.calls[0].event_id)
                assert row is not None
                assert row.published_at is None and row.claim_token is None
                assert row.last_error_code == "PUBLISH_FAILED"
                row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
            publisher.error = asyncio.CancelledError()
            with pytest.raises(asyncio.CancelledError):
                await relay.once()
            async with service.factory() as session:
                row = await session.get(MessageOutbox, publisher.calls[0].event_id)
                assert row is not None
                assert row.lease_until and row.claim_token
                assert row.published_at is None and row.attempts == 2

    asyncio.run(scenario())


def test_locked_head_skips_room_not_sequence_and_stale_claim_cannot_finish(
    postgres_url: str,
) -> None:
    async def scenario():
        async with service_database(postgres_url) as service:
            room = uuid4()
            async with service.factory() as session, session.begin():
                session.add(Conversation(id=room, kind="dm"))
                await session.flush()
                session.add_all(
                    [Member(conversation_id=room, user_id=user) for user in (A, B)]
                )
            await service.store(A, SEED_ROOM, uuid4(), MessagePayload("one"))
            await service.store(A, SEED_ROOM, uuid4(), MessagePayload("two"))
            await service.store(A, room, uuid4(), MessagePayload("other"))
            async with service.factory() as s1, s1.begin():
                claim1 = await RelayRepository(s1).claim()
                assert claim1 and claim1.conversation_id == SEED_ROOM
                async with service.factory() as s2, s2.begin():
                    claim2 = await RelayRepository(s2).claim()
                    assert claim2 and claim2.conversation_id == room
            async with service.factory() as session, session.begin():
                assert await RelayRepository(session).claim() is None
                await session.execute(
                    update(MessageOutbox)
                    .where(MessageOutbox.event_id == claim1.event_id)
                    .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
                )
            async with service.factory() as session, session.begin():
                reclaimed = await RelayRepository(session).claim()
                assert reclaimed and reclaimed.event_id == claim1.event_id
                assert reclaimed.token != claim1.token
            async with service.factory() as session, session.begin():
                assert not await RelayRepository(session).finish(claim1)
                assert await RelayRepository(session).finish(reclaimed)
            async with service.factory() as session, session.begin():
                next_claim = await RelayRepository(session).claim()
                assert next_claim and next_claim.payload["message"]["seq"] == "2"

    asyncio.run(scenario())


def test_network_io_holds_no_database_transaction(postgres_url: str) -> None:
    async def scenario():
        async with service_database(postgres_url) as service:
            await service.store(A, SEED_ROOM, uuid4(), MessagePayload("one"))

            class Probe:
                async def publish(self, claim):
                    async with service.factory() as session, session.begin():
                        # NOWAIT would fail if the claim transaction stayed open.
                        row = await session.scalar(
                            select(MessageOutbox)
                            .where(MessageOutbox.event_id == claim.event_id)
                            .with_for_update(nowait=True)
                        )
                        assert row is not None
                        assert (
                            row.claim_token == claim.token and row.published_at is None
                        )

            assert await OutboxRelay(service.factory, Probe()).once() == "published"

    asyncio.run(scenario())


def test_ack_then_process_loss_republishes_same_event_after_lease(
    postgres_url: str,
) -> None:
    async def scenario():
        async with service_database(postgres_url) as service:
            await service.store(A, SEED_ROOM, uuid4(), MessagePayload("ack window"))
            # 독립 broker가 기록한 직후 프로세스가 사라진 구간을 모사합니다.
            publisher = Publisher(asyncio.CancelledError())
            relay = OutboxRelay(service.factory, publisher)
            with pytest.raises(asyncio.CancelledError):
                await relay.once()
            assert await relay.once() == "idle"
            async with service.factory() as session, session.begin():
                await session.execute(
                    update(MessageOutbox).values(
                        lease_until=datetime.now(UTC) - timedelta(seconds=1)
                    )
                )
            publisher.error = None
            assert await relay.once() == "published"
            assert len(publisher.calls) == 2
            assert publisher.calls[0].event_id == publisher.calls[1].event_id
            assert publisher.calls[0].payload == publisher.calls[1].payload
            assert publisher.calls[0].token != publisher.calls[1].token

    asyncio.run(scenario())
