import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command, op
from alembic.config import Config
from sqlalchemy import MetaData, insert, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from tests.postgres_support import validate_test_url

from chat_service.domain.chat import MessagePayload, payload_fingerprint
from chat_service.models.chat import metadata

pytestmark = pytest.mark.postgres
SERVICE_ROOT = Path(__file__).resolve().parents[2]


def migration_config(schema: str) -> Config:
    config = Config(str(SERVICE_ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    return config


async def migrate(engine: AsyncEngine, config: Config, action: str) -> None:
    def run(connection: Connection) -> None:
        config.attributes["connection"] = connection
        if action == "check":
            command.check(config)
        elif action == "down":
            command.downgrade(config, "base")
        else:
            command.upgrade(config, "head")

    try:
        async with engine.begin() as connection:
            await connection.run_sync(run)
    finally:
        config.attributes.pop("connection", None)


@asynccontextmanager
async def isolated_schema(url: str) -> AsyncIterator[tuple[AsyncEngine, str]]:
    validate_test_url(url)
    engine = create_async_engine(
        url,
        hide_parameters=True,
        connect_args={
            "timeout": 3,
            "server_settings": {"statement_timeout": "5000", "lock_timeout": "1000"},
        },
    )
    schema = "run_" + uuid4().hex
    try:
        async with engine.begin() as connection:
            identity = (
                await connection.execute(
                    text("SELECT current_database(),current_user,pg_is_in_recovery()")
                )
            ).one()
            if tuple(identity) != ("laughtale_chat_test", "chat_test", False):
                raise RuntimeError("Unexpected test database")
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        try:
            yield engine, schema
        finally:
            async with engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        await engine.dispose()


def test_migration_roundtrip_and_model_parity(postgres_url: str) -> None:
    async def scenario() -> None:
        async with isolated_schema(postgres_url) as (engine, schema):
            config = migration_config(schema)
            await migrate(engine, config, "up")
            await migrate(engine, config, "up")
            await migrate(engine, config, "check")
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        text(f'SELECT version_num FROM "{schema}".alembic_version')
                    )
                    == "0001"
                )
            await migrate(engine, config, "down")
            async with engine.connect() as connection:
                names = (
                    (
                        await connection.execute(
                            text(
                                "SELECT tablename FROM pg_tables WHERE schemaname=:schema"
                            ),
                            {"schema": schema},
                        )
                    )
                    .scalars()
                    .all()
                )
                assert names == ["alembic_version"]
                assert (
                    await connection.scalar(
                        text(f'SELECT count(*) FROM "{schema}".alembic_version')
                    )
                    == 0
                )
            await migrate(engine, config, "up")
            await migrate(engine, config, "check")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case,expected",
    [
        ("duplicate_seq", "23505"),
        ("duplicate_key", "23505"),
        ("nonmember", "23503"),
        ("missing_conversation", "23503"),
        ("zero_seq", "23514"),
        ("empty_text", "23514"),
        ("long_text", "23514"),
        ("bad_version", "23514"),
        ("bad_hash", "23514"),
        ("null_text", "23502"),
        ("negative_counter", "23514"),
        ("unsupported_kind", "23514"),
        ("duplicate_member", "23505"),
        ("missing_user", "23503"),
    ],
)
def test_constraints_reject_invalid_writes(
    postgres_url: str, case: str, expected: str
) -> None:
    async def scenario() -> None:
        async with isolated_schema(postgres_url) as (engine, schema):
            await migrate(engine, migration_config(schema), "up")
            mapped = MetaData()
            for table in metadata.sorted_tables:
                table.to_metadata(mapped, schema=schema)
            users, conversations, members, messages = (
                mapped.tables[f"{schema}.{name}"]
                for name in ("users", "conversations", "members", "messages")
            )
            actor, outsider, room, key = uuid4(), uuid4(), uuid4(), uuid4()
            payload = MessagePayload("😀" * 2000)
            original = dict(
                id=uuid4(),
                conversation_id=room,
                sender_id=actor,
                client_message_id=key,
                seq=1,
                text=payload.text,
                payload_version=1,
                payload_hash=payload_fingerprint(payload),
            )
            async with engine.begin() as connection:
                await connection.execute(
                    insert(users),
                    [
                        {"id": actor, "display_name": "a"},
                        {"id": outsider, "display_name": "b"},
                    ],
                )
                await connection.execute(insert(conversations).values(id=room))
                await connection.execute(
                    insert(members).values(conversation_id=room, user_id=actor)
                )
                created = await connection.scalar(
                    insert(messages).values(**original).returning(messages.c.created_at)
                )
                assert created is not None and created.utcoffset() is not None
            values = {**original, "id": uuid4(), "client_message_id": uuid4(), "seq": 2}
            overrides = {
                "duplicate_seq": {"seq": 1},
                "duplicate_key": {"client_message_id": key},
                "nonmember": {"sender_id": outsider},
                "missing_conversation": {"conversation_id": uuid4()},
                "zero_seq": {"seq": 0},
                "empty_text": {"text": ""},
                "long_text": {"text": "😀" * 2001},
                "bad_version": {"payload_version": 2},
                "bad_hash": {"payload_hash": "invalid"},
                "null_text": {"text": None},
            }
            statement = insert(messages).values(**(values | overrides.get(case, {})))
            if case == "negative_counter":
                statement = insert(conversations).values(id=uuid4(), last_seq=-1)
            elif case == "unsupported_kind":
                statement = insert(conversations).values(id=uuid4(), kind="group")
            elif case in {"duplicate_member", "missing_user"}:
                statement = insert(members).values(
                    conversation_id=room,
                    user_id=actor if case == "duplicate_member" else uuid4(),
                )
            with pytest.raises(IntegrityError) as error:
                async with engine.begin() as connection:
                    await connection.execute(statement)
            assert getattr(error.value.orig, "sqlstate", None) == expected
            async with engine.connect() as connection:
                rows = (await connection.execute(select(messages))).mappings().all()
                assert (
                    len(rows) == 1
                    and rows[0]["text"] == payload.text
                    and rows[0]["seq"] == 1
                )

    asyncio.run(scenario())


def test_migration_failure_rolls_back_ddl(postgres_url: str, monkeypatch) -> None:
    create_table = op.create_table

    def fail_on_message(name, *args, **kwargs):
        if name == "messages":
            raise RuntimeError("synthetic migration failure")
        return create_table(name, *args, **kwargs)

    monkeypatch.setattr(op, "create_table", fail_on_message)

    async def scenario() -> None:
        async with isolated_schema(postgres_url) as (engine, schema):
            with pytest.raises(RuntimeError, match="Migration failed"):
                await migrate(engine, migration_config(schema), "up")
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        text("SELECT count(*) FROM pg_tables WHERE schemaname=:schema"),
                        {"schema": schema},
                    )
                    == 0
                )

    asyncio.run(scenario())


def test_test_role_cannot_migrate_default_schema(postgres_url: str) -> None:
    async def scenario() -> None:
        async with isolated_schema(postgres_url) as (engine, _):
            with pytest.raises(RuntimeError, match="Migration failed"):
                await migrate(engine, migration_config("chat"), "up")

    asyncio.run(scenario())
