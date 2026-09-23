import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from platform_contracts.wire import ConnectionSeed, InboundEvent, Profile
from sqlalchemy import func, select
from tests.integration.test_chat_service import A, B, service_database

from chat_service.domain.chat import MessagePayload
from chat_service.exceptions.chat import (
    ConversationNotFoundError,
    IdempotencyConflictError,
)
from chat_service.models.external import (
    ExternalInboundEvent,
    ExternalMessage,
    ExternalOutboundJob,
)
from chat_service.services.external import ExternalService

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 9, 8, tzinfo=UTC)


def seed_input(connection=None):
    return ConnectionSeed(
        run_id="test",
        profile=Profile.TELEGRAM,
        connection_id=connection or uuid4(),
        external_conversation_id="room",
        external_sender_id="customer",
        operator_user_id=A,
    )


def event_input(seed, **changes):
    values = dict(
        run_id=seed.run_id,
        profile=seed.profile,
        connection_id=seed.connection_id,
        external_conversation_id=seed.external_conversation_id,
        external_sender_id=seed.external_sender_id,
        external_event_id="e-1",
        external_message_id="m-1",
        text="합성 고객",
        occurred_at=NOW,
    )
    return InboundEvent.model_validate(values | changes)


def test_external_ingress_egress_and_permissions(postgres_url: str):
    async def scenario():
        async with service_database(postgres_url) as database:
            seed = seed_input()
            service = ExternalService(
                database, lambda: NOW, connection_ids=frozenset({seed.connection_id})
            )
            room = await service.seed(seed)
            assert await service.seed(seed) == room
            event = event_input(seed)
            first = await service.receive(event)
            replay = await service.receive(event)
            assert replay.replay and replay.message == first.message
            assert first.message.external_sender_id == "customer"
            # 다른 event_id로 같은 메시지가 재수신되어도 seq를 소비하지 않습니다.
            alias = await service.receive(event_input(seed, external_event_id="e-2"))
            assert alias.replay and alias.message.seq == 1
            with pytest.raises(IdempotencyConflictError):
                await service.receive(event_input(seed, text="changed"))
            with pytest.raises(IdempotencyConflictError):
                await service.receive(
                    event_input(seed, external_event_id="e-3", text="changed")
                )
            with pytest.raises(ConversationNotFoundError):
                await service.send(B, room, uuid4(), MessagePayload("unauthorized"))
            assert await service.rooms(B) == ()
            key = uuid4()
            sent = await service.send(A, room, key, MessagePayload("reply"))
            assert sent.message.seq == 2 and sent.message.delivery_state == "pending"
            assert (await service.send(A, room, key, MessagePayload("reply"))).replay
            with pytest.raises(IdempotencyConflictError):
                await service.send(A, room, key, MessagePayload("changed"))
            history = await service.history(A, room, 0, None, 1)
            assert history.has_more and history.snapshot_head_seq == 2
            assert (await service.history(A, room, 1, 2, 100)).messages[
                0
            ].operation_id == sent.message.operation_id
            async with database.factory() as session:
                assert (
                    await session.scalar(
                        select(func.count()).select_from(ExternalInboundEvent)
                    )
                    == 2
                )
                assert (
                    await session.scalar(
                        select(func.count()).select_from(ExternalOutboundJob)
                    )
                    == 1
                )
                assert (
                    await session.scalar(
                        select(func.count()).select_from(ExternalMessage)
                    )
                    == 2
                )

    asyncio.run(scenario())


def test_parallel_duplicates_and_cross_connection_ids(postgres_url: str):
    async def scenario():
        async with service_database(postgres_url) as database:
            seed, other = seed_input(), seed_input()
            service = ExternalService(
                database,
                lambda: NOW,
                connection_ids=frozenset({seed.connection_id, other.connection_id}),
            )
            room = await service.seed(seed)
            results = await asyncio.gather(
                *(service.receive(event_input(seed)) for _ in range(4))
            )
            assert sum(not item.replay for item in results) == 1
            other_room = await service.seed(other)
            assert other_room != room
            assert (await service.receive(event_input(other))).message.seq == 1
            key = uuid4()
            sent = await asyncio.gather(
                *(service.send(A, room, key, MessagePayload("reply")) for _ in range(4))
            )
            assert sum(not item.replay for item in sent) == 1
            assert await service.head(A, room) == 2
            empty = ExternalService(database, lambda: NOW, connection_ids=frozenset())
            assert await empty.rooms(A) == ()
            with pytest.raises(ConversationNotFoundError):
                await empty.send(A, room, uuid4(), MessagePayload("outside"))
            with pytest.raises(ConversationNotFoundError):
                await empty.receive(event_input(seed))
            with pytest.raises(ConversationNotFoundError):
                await empty.seed(seed)

    asyncio.run(scenario())
