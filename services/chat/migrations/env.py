"""명시적 migration만 실행하며 앱의 설정을 재사용하지 않습니다."""

import asyncio
import os
import re

from alembic import context
from sqlalchemy import MetaData, pool, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from chat_service.models import external, sessions  # noqa: F401
from chat_service.models.chat import metadata

config = context.config
schema = config.attributes.get("schema", "chat")
if not isinstance(schema, str) or not (
    schema == "chat" or re.fullmatch(r"run_[0-9a-f]{32}", schema)
):
    raise RuntimeError("Unexpected migration schema")

target_metadata = MetaData()
for table in metadata.sorted_tables:
    table.to_metadata(target_metadata, schema=schema)


def do_run_migrations(connection: Connection) -> None:
    database, actor, recovery = connection.execute(
        text("SELECT current_database(), current_user, pg_is_in_recovery()")
    ).one()
    if recovery:
        raise RuntimeError("Migration requires Primary")
    if database == "laughtale_chat_test" and actor == "chat_test" and schema != "chat":
        pass
    elif (
        database == "laughtale_chat"
        and schema == "chat"
        and actor in {"chat_owner", "chat_migrator"}
    ):
        connection.execute(text("SET LOCAL ROLE chat_owner"))
    else:
        raise RuntimeError("Unexpected migration database or role")
    owner = connection.scalar(
        text(
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname=:schema"
        ),
        {"schema": schema},
    )
    expected_owner = "chat_test" if database == "laughtale_chat_test" else "chat_owner"
    if owner != expected_owner:
        raise RuntimeError(
            "Migration schema must already exist with its expected owner"
        )
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=schema,
        include_schemas=True,
        include_name=lambda name, kind, parents: kind != "schema" or name == schema,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    try:
        url = make_url(os.environ.get("CHAT_MIGRATION_URL", ""))
    except Exception:
        raise RuntimeError("Set a dedicated CHAT_MIGRATION_URL") from None
    if (
        (url.drivername, url.host, url.port, url.database, url.username)
        != ("postgresql+asyncpg", "127.0.0.1", 5440, "laughtale_chat", "chat_migrator")
        or not url.password
        or url.query
        or schema != "chat"
    ):
        raise RuntimeError("Only the dedicated lab migration account is allowed")
    engine = create_async_engine(
        url,
        poolclass=pool.NullPool,
        hide_parameters=True,
        connect_args={
            "timeout": 3,
            "server_settings": {"statement_timeout": "10000", "lock_timeout": "1000"},
        },
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


try:
    if context.is_offline_mode():
        if schema != "chat":
            raise RuntimeError("Offline SQL targets the chat schema only")
        context.configure(
            dialect_name="postgresql",
            target_metadata=target_metadata,
            version_table_schema=schema,
            literal_binds=True,
        )
        with context.begin_transaction():
            context.execute("SET LOCAL ROLE chat_owner")
            context.run_migrations()
    elif config.attributes.get("connection") is not None:
        do_run_migrations(config.attributes["connection"])
    else:
        asyncio.run(run_async_migrations())
except Exception as error:
    # CLI 경계에서 접속 정보와 SQL 파라미터를 표시하지 않습니다.
    raise RuntimeError(f"Migration failed: {type(error).__name__}") from None
