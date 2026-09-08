import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.integration.test_chat_schema import (
    isolated_schema,
    migrate,
    migration_config,
)

from chat_service.domain.chat import MessagePayload, payload_fingerprint
from chat_service.models.chat import Conversation, Member, Message, User

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("rollback", [False, True])
def test_orm_roundtrip_and_rollback(postgres_url: str, rollback: bool) -> None:
    async def scenario() -> None:
        async with isolated_schema(postgres_url) as (engine, schema):
            await migrate(engine, migration_config(schema), "up")
            factory = async_sessionmaker(
                engine.execution_options(schema_translate_map={"chat": schema}),
                expire_on_commit=False,
            )
            actor, room, message_id = uuid4(), uuid4(), uuid4()
            payload = MessagePayload("  원문😀\n")
            async with factory() as session, session.begin():
                session.add_all(
                    [User(id=actor, display_name="a"), Conversation(id=room)]
                )
                await session.flush()
                session.add(Member(conversation_id=room, user_id=actor))
            async with factory() as session:
                conversation = await session.get(Conversation, room)
                assert isinstance(conversation, Conversation)
                assert conversation.kind == "dm" and conversation.last_seq == 0

            async def write() -> None:
                async with factory() as session, session.begin():
                    message = Message(
                        id=message_id,
                        conversation_id=room,
                        sender_id=actor,
                        client_message_id=uuid4(),
                        seq=1,
                        text=payload.text,
                        payload_version=payload.version,
                        payload_hash=payload_fingerprint(payload),
                    )
                    session.add(message)
                    await session.flush()
                    assert message.created_at.utcoffset() is not None
                    if rollback:
                        raise ValueError("synthetic rollback after flush")

            if rollback:
                with pytest.raises(ValueError, match="synthetic rollback"):
                    await write()
            else:
                await write()

            async with factory() as session:
                loaded = await session.scalar(
                    select(Message).where(Message.id == message_id)
                )
            if rollback:
                assert loaded is None
            else:
                assert isinstance(loaded, Message)
                # Session 종료 후 이미 조회한 값 접근이 SQL을 추가 실행하지 않습니다.
                statements = []

                def record(*args) -> None:
                    statements.append(True)

                event.listen(engine.sync_engine, "before_cursor_execute", record)
                try:
                    assert loaded.id == message_id
                    assert loaded.text == payload.text
                    assert loaded.created_at.utcoffset() is not None
                    assert loaded.seq == 1
                    assert statements == []
                finally:
                    event.remove(engine.sync_engine, "before_cursor_execute", record)
            await migrate(engine, migration_config(schema), "check")

    asyncio.run(scenario())
