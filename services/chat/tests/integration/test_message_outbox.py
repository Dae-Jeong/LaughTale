import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from tests.integration.test_chat_service import SEED_ROOM, A, B, service_database

from chat_service.domain.chat import MessagePayload
from chat_service.exceptions.database import DatabaseBusy
from chat_service.models.chat import Conversation, Message, MessageOutbox
from chat_service.schemas.chat import MessageCreated

pytestmark = pytest.mark.postgres


def test_message_and_event_commit_once_under_retries(postgres_url: str) -> None:
    async def scenario() -> None:
        async with service_database(postgres_url) as service:
            key = uuid4()
            results = await asyncio.gather(
                *[
                    service.store(A, SEED_ROOM, key, MessagePayload("hello😀"))
                    for _ in range(6)
                ]
            )
            assert sum(not result.replay for result in results) == 1
            async with service.transaction() as repository:
                rows = (await repository.session.scalars(select(MessageOutbox))).all()
                assert len(rows) == 1
                stored = results[0].message
                event = MessageCreated.model_validate(rows[0].payload)
                assert rows[0].event_id == event.event_id == stored.message_id
                assert event.message.seq == str(stored.seq)
                assert event.message.text == stored.text
                assert event.message.created_at == stored.created_at

    asyncio.run(scenario())


def test_event_failure_rolls_back_message_and_counter(
    postgres_url: str, monkeypatch
) -> None:
    def fail(_message):
        raise ValueError("injected serialization failure")

    monkeypatch.setattr("chat_service.repositories.chat.message_created", fail)

    async def scenario() -> None:
        async with service_database(postgres_url) as service:
            with pytest.raises(ValueError, match="injected"):
                await service.store(A, SEED_ROOM, uuid4(), MessagePayload("rollback"))
            async with service.transaction() as repository:
                for model in (Message, MessageOutbox):
                    assert (
                        await repository.session.scalar(
                            select(func.count()).select_from(model)
                        )
                        == 0
                    )
                assert (
                    await repository.session.scalar(select(Conversation.last_seq)) == 0
                )

    asyncio.run(scenario())


def test_capacity_rejects_new_work_but_preserves_replay(
    postgres_url: str, monkeypatch
) -> None:
    monkeypatch.setattr("chat_service.repositories.chat.OUTBOX_ROW_LIMIT", 1)

    async def scenario() -> None:
        async with service_database(postgres_url) as service:
            key = uuid4()
            await service.store(A, SEED_ROOM, key, MessagePayload("stored"))
            assert (
                await service.store(A, SEED_ROOM, key, MessagePayload("stored"))
            ).replay
            with pytest.raises(DatabaseBusy):
                await service.store(B, SEED_ROOM, uuid4(), MessagePayload("full"))
            assert await service.head(A, SEED_ROOM) == 1
            async with service.transaction() as repository:
                assert (
                    await repository.session.scalar(
                        select(func.count()).select_from(MessageOutbox)
                    )
                    == 1
                )

    asyncio.run(scenario())


def test_cancel_rolls_back_both_rows(postgres_url: str) -> None:
    async def scenario() -> None:
        async with service_database(postgres_url) as service:
            with pytest.raises(asyncio.CancelledError):
                async with service.transaction() as repository:
                    await repository.authorized_head(A, SEED_ROOM, lock=True)
                    await repository.append(
                        A, SEED_ROOM, uuid4(), MessagePayload("cancel"), 1
                    )
                    raise asyncio.CancelledError()
            async with service.transaction() as repository:
                for model in (Message, MessageOutbox):
                    assert (
                        await repository.session.scalar(
                            select(func.count()).select_from(model)
                        )
                        == 0
                    )
            assert await service.head(A, SEED_ROOM) == 0

    asyncio.run(scenario())
