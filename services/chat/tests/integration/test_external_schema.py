"""외부 원장의 계정·권한·멱등 제약을 격리된 실제 PostgreSQL로 검증합니다."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import MetaData, insert, select, text
from sqlalchemy.exc import IntegrityError
from tests.integration.test_chat_schema import (
    isolated_schema,
    migrate,
    migration_config,
)

from chat_service.models.base import Base
from chat_service.models.external import ExternalMessage

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    "case,code",
    [
        ("duplicate_room", "23505"),
        ("duplicate_participant", "23505"),
        ("duplicate_seq", "23505"),
        ("duplicate_message", "23505"),
        ("other_connection_participant", "23503"),
        ("other_connection_room", "23503"),
        ("operator_not_member", "23503"),
        ("two_senders", "23514"),
        ("no_sender", "23514"),
        ("zero_seq", "23514"),
        ("duplicate_event", "23505"),
        ("other_connection_event", "23503"),
        ("duplicate_client", "23505"),
        ("accepted_without_effect", "23514"),
        ("sending_without_lease", "23514"),
        ("pending_with_lease", "23514"),
        ("negative_attempts", "23514"),
        ("duplicate_job", "23505"),
    ],
)
def test_external_constraints(postgres_url: str, case: str, code: str) -> None:
    async def scenario() -> None:
        async with isolated_schema(postgres_url) as (engine, schema):
            await migrate(engine, migration_config(schema), "up")
            mapped = MetaData()
            for table in Base.metadata.sorted_tables:
                table.to_metadata(mapped, schema=schema)
            tables = {table.name: table for table in mapped.tables.values()}
            c1, c2, room, p1, p2, actor, outsider = (uuid4() for _ in range(7))
            incoming, outgoing, key = uuid4(), uuid4(), uuid4()
            now = datetime.now(UTC)
            customer = dict(
                id=incoming,
                conversation_id=room,
                connection_id=c1,
                participant_id=p1,
                external_message_id="message-1",
                seq=1,
                text="합성 메시지",
                payload_hash="a" * 64,
                occurred_at=now,
                received_at=now,
            )
            operator = dict(
                id=outgoing,
                conversation_id=room,
                connection_id=c1,
                operator_user_id=actor,
                client_message_id=key,
                seq=2,
                text="합성 답장",
                payload_hash="b" * 64,
                occurred_at=now,
                received_at=now,
            )
            async with engine.begin() as connection:
                await connection.execute(
                    insert(tables["users"]),
                    [
                        {"id": actor, "display_name": "operator"},
                        {"id": outsider, "display_name": "outsider"},
                    ],
                )
                await connection.execute(
                    insert(tables["external_connections"]),
                    [
                        {"id": c1, "profile": "telegram", "run_id": "test"},
                        {"id": c2, "profile": "telegram", "run_id": "test"},
                    ],
                )
                await connection.execute(
                    insert(tables["external_conversations"]),
                    [
                        {
                            "id": room,
                            "connection_id": c1,
                            "external_conversation_id": "same-room",
                        },
                        {
                            "id": uuid4(),
                            "connection_id": c2,
                            "external_conversation_id": "same-room",
                        },
                    ],
                )
                await connection.execute(
                    insert(tables["external_participants"]),
                    [
                        {
                            "id": p1,
                            "connection_id": c1,
                            "external_sender_id": "same-person",
                        },
                        {
                            "id": p2,
                            "connection_id": c2,
                            "external_sender_id": "same-person",
                        },
                    ],
                )
                await connection.execute(
                    insert(tables["external_members"]).values(
                        conversation_id=room, user_id=actor
                    )
                )
                await connection.execute(
                    insert(tables["external_messages"]).values(**customer)
                )
                await connection.execute(
                    insert(tables["external_messages"]).values(**operator)
                )
                await connection.execute(
                    insert(tables["external_inbound_events"]).values(
                        connection_id=c1,
                        event_id="event-1",
                        message_id=incoming,
                        payload_hash="a" * 64,
                    )
                )
                await connection.execute(
                    insert(tables["external_outbound_jobs"]).values(
                        id=uuid4(), message_id=outgoing, next_attempt_at=now
                    )
                )
            values = customer | {
                "id": uuid4(),
                "seq": 3,
                "external_message_id": "message-2",
            }
            changes = {
                "duplicate_seq": {"seq": 1},
                "duplicate_message": {"external_message_id": "message-1"},
                "other_connection_participant": {"participant_id": p2},
                "other_connection_room": {"connection_id": c2, "participant_id": p2},
                "two_senders": {"operator_user_id": actor},
                "no_sender": {"participant_id": None},
                "zero_seq": {"seq": 0},
            }
            statement = insert(tables["external_messages"]).values(
                **(values | changes.get(case, {}))
            )
            if case == "duplicate_room":
                statement = insert(tables["external_conversations"]).values(
                    id=uuid4(), connection_id=c1, external_conversation_id="same-room"
                )
            elif case == "duplicate_participant":
                statement = insert(tables["external_participants"]).values(
                    id=uuid4(), connection_id=c1, external_sender_id="same-person"
                )
            elif case in {"operator_not_member", "duplicate_client"}:
                statement = insert(tables["external_messages"]).values(
                    **(
                        operator
                        | {
                            "id": uuid4(),
                            "seq": 3,
                            "operator_user_id": outsider
                            if case == "operator_not_member"
                            else actor,
                        }
                    )
                )
            elif case in {"duplicate_event", "other_connection_event"}:
                statement = insert(tables["external_inbound_events"]).values(
                    connection_id=c1 if case == "duplicate_event" else c2,
                    event_id="event-1",
                    message_id=incoming,
                    payload_hash="a" * 64,
                )
            elif case in {
                "accepted_without_effect",
                "sending_without_lease",
                "pending_with_lease",
                "negative_attempts",
                "duplicate_job",
            }:
                job_changes = {
                    "accepted_without_effect": {"state": "accepted"},
                    "sending_without_lease": {"state": "sending"},
                    "pending_with_lease": {
                        "lease_token": uuid4(),
                        "lease_expires_at": now,
                    },
                    "negative_attempts": {"attempt_count": -1},
                }
                # 기존 job을 수정해 UNIQUE가 CHECK보다 먼저 실패하는 모호함을 제거합니다.
                job = tables["external_outbound_jobs"]
                if case == "duplicate_job":
                    statement = insert(job).values(
                        id=uuid4(), message_id=outgoing, next_attempt_at=now
                    )
                else:
                    statement = (
                        job.update()
                        .where(job.c.message_id == outgoing)
                        .values(**job_changes[case])
                    )
            with pytest.raises(IntegrityError) as error:
                async with engine.begin() as connection:
                    await connection.execute(statement)
            assert getattr(error.value.orig, "sqlstate", None) == code
            async with engine.connect() as connection:
                assert (
                    len(
                        (
                            await connection.execute(
                                select(tables["external_messages"])
                            )
                        ).all()
                    )
                    == 2
                )
                assert not (await connection.execute(select(tables["messages"]))).all()

    asyncio.run(scenario())


def test_external_downgrade_preserves_internal_schema(postgres_url: str) -> None:
    async def scenario() -> None:
        async with isolated_schema(postgres_url) as (engine, schema):
            config = migration_config(schema)
            await migrate(engine, config, "up")
            async with engine.begin() as connection:
                await connection.execute(
                    text(f'INSERT INTO "{schema}".users VALUES (:id, :name)'),
                    {"id": uuid4(), "name": "preserved"},
                )

                def down(sync_connection):
                    config.attributes["connection"] = sync_connection
                    command.downgrade(config, "0001")

                await connection.run_sync(down)
            config.attributes.pop("connection", None)
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        text(f'SELECT count(*) FROM "{schema}".users')
                    )
                    == 1
                )
                assert (
                    await connection.scalar(
                        text(f'SELECT version_num FROM "{schema}".alembic_version')
                    )
                    == "0001"
                )
            await migrate(engine, config, "up")
            await migrate(engine, config, "check")

    assert ExternalMessage.__tablename__ == "external_messages"
    asyncio.run(scenario())
