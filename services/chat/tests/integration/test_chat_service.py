import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.integration.test_chat_schema import (
    isolated_schema,
    migrate,
    migration_config,
)

from chat_service.contracts.chat import Actor
from chat_service.core.chat_hub import ChatHub, Peer
from chat_service.core.clock import system_clock
from chat_service.core.database_metrics import create_database_metrics
from chat_service.core.metrics import create_metrics
from chat_service.core.sessions import (
    SEED_ROOM,
    SYNTHETIC_USERS,
    LocalSessions,
    LocalSessionStore,
)
from chat_service.domain.chat import MessagePayload
from chat_service.exceptions.chat import (
    ConversationNotFoundError,
    IdempotencyConflictError,
    InvalidCursorError,
)
from chat_service.models.chat import Conversation, Message
from chat_service.repositories.chat import ChatRepository
from chat_service.seed import seed_rows
from chat_service.services.chat import ChatService

pytestmark = pytest.mark.postgres
A, B = SYNTHETIC_USERS.values()


@asynccontextmanager
async def service_database(url: str) -> AsyncIterator[ChatService]:
    async with isolated_schema(url) as (engine, schema):
        await migrate(engine, migration_config(schema), "up")
        factory = async_sessionmaker(
            engine.execution_options(schema_translate_map={"chat": schema}),
            expire_on_commit=False,
        )
        async with factory() as session, session.begin():
            await seed_rows(session)
            await seed_rows(session)
        yield ChatService(factory, create_database_metrics(create_metrics(), 5))


def test_store_replay_conflict_authorization_and_snapshots(postgres_url: str) -> None:
    async def scenario() -> None:
        async with service_database(postgres_url) as service:
            key = uuid4()
            first = await service.store(A, SEED_ROOM, key, MessagePayload("  보존😀\n"))
            replay = await service.store(
                A, SEED_ROOM, key, MessagePayload("  보존😀\n")
            )
            assert (
                first.message == replay.message and replay.replay and not first.replay
            )
            with pytest.raises(IdempotencyConflictError):
                await service.store(A, SEED_ROOM, key, MessagePayload("다름"))
            with pytest.raises(ConversationNotFoundError):
                await service.store(uuid4(), SEED_ROOM, key, MessagePayload("다름"))
            with pytest.raises(ConversationNotFoundError):
                await service.history(uuid4(), SEED_ROOM, 0, None, 50)
            assert await service.rooms(uuid4()) == ()
            assert (await service.rooms(A))[0].title == "User B"
            second = await service.store(
                B, SEED_ROOM, key, MessagePayload("same key other actor")
            )
            assert second.message.seq == 2
            page = await service.history(A, SEED_ROOM, 0, None, 1)
            assert (
                page.next_cursor == 1 and page.snapshot_head_seq == 2 and page.has_more
            )
            await service.store(B, SEED_ROOM, uuid4(), MessagePayload("new tail"))
            last = await service.history(A, SEED_ROOM, 1, page.snapshot_head_seq, 1)
            assert last.next_cursor == 2 and not last.has_more
            empty = await service.history(A, SEED_ROOM, 2, 2, 100)
            assert empty.next_cursor == 2 and not empty.messages
            for after, head in ((4, None), (0, 4), (2, 1)):
                with pytest.raises(InvalidCursorError):
                    await service.history(A, SEED_ROOM, after, head, 1)

    asyncio.run(scenario())


def test_concurrent_same_and_different_keys(postgres_url: str) -> None:
    async def scenario() -> None:
        async with service_database(postgres_url) as service:
            key = uuid4()
            barrier = asyncio.Barrier(4)

            async def store(same: bool):
                await barrier.wait()
                return await service.store(
                    A, SEED_ROOM, key if same else uuid4(), MessagePayload("race")
                )

            results = await asyncio.gather(*(store(True) for _ in range(4)))
            assert sum(not item.replay for item in results) == 1
            assert len({item.message.message_id for item in results}) == 1
            results = await asyncio.gather(*(store(False) for _ in range(4)))
            assert sorted(item.message.seq for item in results) == [2, 3, 4, 5]
            page = await service.history(A, SEED_ROOM, 0, None, 100)
            assert [item.seq for item in page.messages] == [1, 2, 3, 4, 5]

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["rollback", "cancel"])
def test_uncommitted_writer_blocks_later_seq_and_rollback_reuses_seq(
    postgres_url: str, mode: str
) -> None:
    async def scenario() -> None:
        async with service_database(postgres_url) as service:
            entered = asyncio.Event()
            release = asyncio.Event()

            async def first_writer():
                async with service.transaction() as repo:
                    head = await repo.authorized_head(A, SEED_ROOM, lock=True)
                    await repo.append(
                        A, SEED_ROOM, uuid4(), MessagePayload("uncommitted"), head + 1
                    )
                    entered.set()
                    await release.wait()
                    raise ValueError("synthetic rollback")

            first = asyncio.create_task(first_writer())
            await entered.wait()
            second = asyncio.create_task(
                service.store(B, SEED_ROOM, uuid4(), MessagePayload("committed"))
            )
            try:
                await asyncio.sleep(0.05)
                assert not second.done()
                assert (
                    await service.history(A, SEED_ROOM, 0, None, 100)
                ).snapshot_head_seq == 0
                if mode == "cancel":
                    first.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await first
                else:
                    release.set()
                    with pytest.raises(ValueError):
                        await first
                result = await second
                assert result.message.seq == 1
                async with service.factory() as session:
                    assert (
                        await session.scalars(select(Message))
                    ).one().text == "committed"
                    room = await session.get(Conversation, SEED_ROOM)
                    assert room is not None and room.last_seq == 1
            finally:
                for task in (first, second):
                    task.cancel()
                await asyncio.gather(first, second, return_exceptions=True)

    asyncio.run(scenario())


def test_flush_failure_rolls_back_counter(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ChatRepository.append

    async def fail(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        raise ValueError("synthetic after flush")

    async def scenario() -> None:
        async with service_database(postgres_url) as service:
            with monkeypatch.context() as patch:
                patch.setattr(ChatRepository, "append", fail)
                with pytest.raises(ValueError):
                    await service.store(
                        A, SEED_ROOM, uuid4(), MessagePayload("rollback")
                    )
            assert (
                await service.store(A, SEED_ROOM, uuid4(), MessagePayload("next"))
            ).message.seq == 1

    asyncio.run(scenario())


def test_two_peers_share_one_primary_head_query(postgres_url: str) -> None:
    async def scenario() -> None:
        async with service_database(postgres_url) as service:
            hub = ChatHub(create_metrics())
            sessions = LocalSessions(system_clock)
            for actor in (A, B):
                hub.join(
                    Peer(
                        actor_id=actor,
                        token=sessions.issue(Actor(actor, "test")),
                        rooms={SEED_ROOM},
                    )
                )
            engine = service.factory.kw["bind"]
            statements = []

            def count(connection, cursor, statement, parameters, context, executemany):
                statements.append(statement)

            event.listen(engine.sync_engine, "before_cursor_execute", count)
            try:
                assert await hub.reconcile(service, LocalSessionStore(sessions))
                assert len(statements) == 1
                assert "members" in statements[0] and "conversations" in statements[0]
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", count)

    asyncio.run(scenario())
