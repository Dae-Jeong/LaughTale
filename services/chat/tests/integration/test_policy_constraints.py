"""앱 입력 정책과 DB 정합성 제약의 책임 분리를 검증합니다."""

import asyncio
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import CheckConstraint, inspect, text, update
from tests.integration.test_chat_schema import (
    isolated_schema,
    migrate,
    migration_config,
)
from tests.integration.test_chat_service import SEED_ROOM, A, service_database

from chat_service.domain.chat import MessagePayload
from chat_service.exceptions.chat import (
    ConversationNotFoundError,
    IdempotencyConflictError,
)
from chat_service.models.base import Base
from chat_service.models.chat import Conversation, Message
from chat_service.seed import seed_rows

pytestmark = pytest.mark.postgres


def test_database_checks_match_models(postgres_url: str) -> None:
    async def scenario() -> None:
        async with isolated_schema(postgres_url) as (engine, schema):
            await migrate(engine, migration_config(schema), "up")
            async with engine.connect() as connection:

                def check(sync):
                    inspector = inspect(sync)
                    for table in Base.metadata.sorted_tables:
                        expected = {
                            c.name
                            for c in table.constraints
                            if isinstance(c, CheckConstraint)
                        }
                        actual = {
                            c["name"]
                            for c in inspector.get_check_constraints(
                                table.name, schema=schema
                            )
                        }
                        assert actual == expected

                await connection.run_sync(check)

    asyncio.run(scenario())


def test_unknown_kind_is_not_exposed_or_written(postgres_url: str) -> None:
    async def scenario() -> None:
        async with service_database(postgres_url) as service:
            async with service.transaction() as repository:
                await repository.session.execute(
                    update(Conversation).values(kind="future")
                )
            assert await service.rooms(A) == ()
            assert await service.heads({A}, {SEED_ROOM}) == {}
            with pytest.raises(ConversationNotFoundError):
                await service.history(A, SEED_ROOM, 0, None, 10)
            with pytest.raises(ConversationNotFoundError):
                await service.store(A, SEED_ROOM, uuid4(), MessagePayload("blocked"))

    asyncio.run(scenario())


def test_unknown_stored_version_is_not_reinterpreted(postgres_url: str) -> None:
    async def scenario() -> None:
        async with service_database(postgres_url) as service:
            key = uuid4()
            await service.store(A, SEED_ROOM, key, MessagePayload("original"))
            async with service.transaction() as repository:
                await repository.session.execute(
                    update(Message).values(payload_version=2)
                )
            with pytest.raises(
                RuntimeError, match="Unsupported stored message version"
            ):
                await service.store(A, SEED_ROOM, key, MessagePayload("original"))
            with pytest.raises(
                RuntimeError, match="Unsupported stored message version"
            ):
                await service.history(A, SEED_ROOM, 0, None, 10)

    asyncio.run(scenario())


def test_old_body_is_not_validated_as_new_input(postgres_url: str) -> None:
    async def scenario() -> None:
        async with service_database(postgres_url) as service:
            key = uuid4()
            await service.store(A, SEED_ROOM, key, MessagePayload("original"))
            async with service.transaction() as repository:
                await repository.session.execute(
                    update(Message).values(text="x" * 2001)
                )
            history = await service.history(A, SEED_ROOM, 0, None, 10)
            assert len(history.messages[0].text) == 2001
            with pytest.raises(IdempotencyConflictError):
                await service.store(A, SEED_ROOM, key, MessagePayload("original"))

    asyncio.run(scenario())


def test_initial_revisions_keep_policy_checks_relaxed(
    postgres_url: str,
) -> None:
    async def scenario() -> None:
        async with isolated_schema(postgres_url) as (engine, schema):
            config = migration_config(schema)

            async def revision(direction: str, target: str) -> None:
                def run(sync):
                    config.attributes["connection"] = sync
                    getattr(command, direction)(config, target)

                try:
                    async with engine.begin() as connection:
                        await connection.run_sync(run)
                finally:
                    config.attributes.pop("connection", None)

            await revision("upgrade", "0001")
            from sqlalchemy.ext.asyncio import async_sessionmaker

            factory = async_sessionmaker(
                engine.execution_options(schema_translate_map={"chat": schema})
            )
            async with factory() as session, session.begin():
                await seed_rows(session)
            await revision("upgrade", "0002")
            async with engine.begin() as connection:
                assert (
                    await connection.scalar(
                        text(f'SELECT count(*) FROM "{schema}".users')
                    )
                    == 2
                )
                await connection.execute(
                    text(f"UPDATE \"{schema}\".conversations SET kind='future'")
                )
            await revision("downgrade", "0001")
            async with engine.begin() as connection:
                assert (
                    await connection.scalar(
                        text(f'SELECT version_num FROM "{schema}".alembic_version')
                    )
                    == "0001"
                )
                assert (
                    await connection.scalar(
                        text(f'SELECT kind FROM "{schema}".conversations')
                    )
                    == "future"
                )
            await revision("upgrade", "0002")

    asyncio.run(scenario())
