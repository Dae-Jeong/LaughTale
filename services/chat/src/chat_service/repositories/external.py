from datetime import datetime
from uuid import UUID, uuid4

from platform_contracts.wire import ConnectionSeed, DeliveryState, InboundEvent, Profile
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from chat_service.contracts.external import (
    ExternalMessageValue,
    ExternalRoom,
    SenderKind,
)
from chat_service.exceptions.chat import (
    ConversationNotFoundError,
    IdempotencyConflictError,
)
from chat_service.exceptions.database import DatabaseBusy
from chat_service.models.chat import User
from chat_service.models.external import (
    ExternalConnection,
    ExternalConversation,
    ExternalInboundEvent,
    ExternalMember,
    ExternalMessage,
    ExternalOutboundJob,
    ExternalParticipant,
)


def message_value(
    message: ExternalMessage,
    job: ExternalOutboundJob | None,
    external_sender_id: str | None = None,
) -> ExternalMessageValue:
    return ExternalMessageValue(
        message.id,
        message.conversation_id,
        SenderKind.CUSTOMER if message.participant_id else SenderKind.OPERATOR,
        str(message.participant_id or message.operator_user_id),
        message.client_message_id,
        message.external_message_id,
        message.seq,
        message.text,
        message.occurred_at,
        message.received_at,
        message.created_at,
        job.id if job else None,
        DeliveryState(job.state) if job else None,
        external_sender_id,
        job.effect_id if job else None,
    )


class ExternalRepository:
    def __init__(
        self, session: AsyncSession, *, connection_ids: frozenset[UUID]
    ) -> None:
        self.session = session
        self.connection_ids = frozenset(connection_ids)

    async def seed(self, value: ConnectionSeed) -> UUID:
        if value.connection_id not in self.connection_ids:
            raise ConversationNotFoundError()
        await self.session.execute(
            insert(ExternalConnection)
            .values(
                id=value.connection_id,
                profile=value.profile.value,
                run_id=value.run_id,
                idempotency_supported=value.idempotency_supported,
                lookup_supported=value.lookup_supported,
            )
            .on_conflict_do_nothing(index_elements=[ExternalConnection.id])
        )
        connection = await self.session.scalar(
            select(ExternalConnection)
            .where(ExternalConnection.id == value.connection_id)
            .with_for_update()
        )
        assert connection is not None
        if (
            connection.profile,
            connection.run_id,
            connection.idempotency_supported,
            connection.lookup_supported,
        ) != (
            value.profile.value,
            value.run_id,
            value.idempotency_supported,
            value.lookup_supported,
        ):
            raise IdempotencyConflictError()
        if await self.session.get(User, value.operator_user_id) is None:
            raise ConversationNotFoundError()
        await self.session.execute(
            insert(ExternalConversation)
            .values(
                id=uuid4(),
                connection_id=value.connection_id,
                external_conversation_id=value.external_conversation_id,
            )
            .on_conflict_do_nothing()
        )
        room = await self.external_room(
            value.connection_id, value.external_conversation_id
        )
        await self.participant(value.connection_id, value.external_sender_id)
        await self.session.execute(
            insert(ExternalMember)
            .values(conversation_id=room.id, user_id=value.operator_user_id)
            .on_conflict_do_nothing()
        )
        return room.id

    async def connection(self, id: UUID) -> ExternalConnection:
        if id not in self.connection_ids:
            raise ConversationNotFoundError()
        connection = await self.session.get(ExternalConnection, id)
        if connection is None:
            raise ConversationNotFoundError()
        return connection

    async def event_lock(self, connection: UUID, event: str) -> None:
        # 해시 충돌은 드물게 직렬화할 뿐 잘못된 중복 성공을 만들지 않습니다.
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"external:{connection}:{event}"},
        )

    async def external_room(
        self, connection: UUID, external_id: str
    ) -> ExternalConversation:
        room = await self.session.scalar(
            select(ExternalConversation)
            .where(
                ExternalConversation.connection_id == connection,
                ExternalConversation.connection_id.in_(self.connection_ids),
                ExternalConversation.external_conversation_id == external_id,
            )
            .with_for_update()
        )
        if room is None:
            raise ConversationNotFoundError()
        return room

    async def authorized_room(
        self, actor: UUID, room: UUID, *, lock: bool = False
    ) -> ExternalConversation:
        query = (
            select(ExternalConversation)
            .join(ExternalMember)
            .where(
                ExternalConversation.id == room,
                ExternalMember.user_id == actor,
                ExternalConversation.connection_id.in_(self.connection_ids),
            )
        )
        if lock:
            query = query.with_for_update(of=ExternalConversation)
        value = await self.session.scalar(query)
        if value is None:
            raise ConversationNotFoundError()
        return value

    async def participant(self, connection: UUID, external_id: str) -> UUID:
        await self.session.execute(
            insert(ExternalParticipant)
            .values(
                id=uuid4(), connection_id=connection, external_sender_id=external_id
            )
            .on_conflict_do_nothing()
        )
        value = await self.session.scalar(
            select(ExternalParticipant.id).where(
                ExternalParticipant.connection_id == connection,
                ExternalParticipant.external_sender_id == external_id,
            )
        )
        assert value is not None
        return value

    async def event(self, connection: UUID, key: str) -> ExternalInboundEvent | None:
        return await self.session.get(ExternalInboundEvent, (connection, key))

    async def by_id(self, message: UUID) -> ExternalMessageValue:
        row = (
            await self.session.execute(
                select(
                    ExternalMessage,
                    ExternalOutboundJob,
                    ExternalParticipant.external_sender_id,
                )
                .outerjoin(ExternalOutboundJob)
                .outerjoin(
                    ExternalParticipant,
                    ExternalMessage.participant_id == ExternalParticipant.id,
                )
                .where(
                    ExternalMessage.id == message,
                    ExternalMessage.connection_id.in_(self.connection_ids),
                )
            )
        ).one_or_none()
        if row is None:
            raise ConversationNotFoundError()
        return message_value(*row)

    async def incoming_existing(self, room: UUID, key: str) -> ExternalMessage | None:
        return await self.session.scalar(
            select(ExternalMessage).where(
                ExternalMessage.conversation_id == room,
                ExternalMessage.external_message_id == key,
            )
        )

    async def outgoing_existing(
        self, room: UUID, actor: UUID, key: UUID
    ) -> ExternalMessage | None:
        return await self.session.scalar(
            select(ExternalMessage).where(
                ExternalMessage.conversation_id == room,
                ExternalMessage.operator_user_id == actor,
                ExternalMessage.client_message_id == key,
            )
        )

    async def incoming(
        self, room: ExternalConversation, event: InboundEvent, hash: str, now: datetime
    ) -> ExternalMessageValue:
        participant = await self.participant(
            event.connection_id, event.external_sender_id
        )
        message = ExternalMessage(
            id=uuid4(),
            conversation_id=room.id,
            connection_id=room.connection_id,
            participant_id=participant,
            external_message_id=event.external_message_id,
            seq=room.last_seq + 1,
            text=event.text,
            payload_hash=hash,
            occurred_at=event.occurred_at,
            received_at=now,
        )
        room.last_seq += 1
        self.session.add(message)
        await self.session.flush()
        return message_value(message, None, event.external_sender_id)

    async def record_event(self, event: InboundEvent, message: UUID, hash: str) -> None:
        self.session.add(
            ExternalInboundEvent(
                connection_id=event.connection_id,
                event_id=event.external_event_id,
                message_id=message,
                payload_hash=hash,
            )
        )
        await self.session.flush()

    async def outgoing(
        self,
        room: ExternalConversation,
        actor: UUID,
        key: UUID,
        value: str,
        hash: str,
        now: datetime,
    ) -> ExternalMessageValue:
        message = ExternalMessage(
            id=uuid4(),
            conversation_id=room.id,
            connection_id=room.connection_id,
            operator_user_id=actor,
            client_message_id=key,
            seq=room.last_seq + 1,
            text=value,
            payload_hash=hash,
            occurred_at=now,
            received_at=now,
        )
        room.last_seq += 1
        self.session.add(message)
        await self.session.flush()
        job = ExternalOutboundJob(
            id=uuid4(), message_id=message.id, next_attempt_at=now
        )
        self.session.add(job)
        await self.session.flush()
        return message_value(message, job)

    async def history(
        self, room: UUID, after: int, head: int, limit: int
    ) -> tuple[ExternalMessageValue, ...]:
        rows = await self.session.execute(
            select(
                ExternalMessage,
                ExternalOutboundJob,
                ExternalParticipant.external_sender_id,
            )
            .outerjoin(ExternalOutboundJob)
            .outerjoin(
                ExternalParticipant,
                ExternalMessage.participant_id == ExternalParticipant.id,
            )
            .where(
                ExternalMessage.conversation_id == room,
                ExternalMessage.seq > after,
                ExternalMessage.seq <= head,
            )
            .order_by(ExternalMessage.seq)
            .limit(limit)
        )
        return tuple(message_value(*row) for row in rows)

    async def rooms(self, actor: UUID) -> tuple[ExternalRoom, ...]:
        rows = await self.session.execute(
            select(ExternalConversation, ExternalConnection)
            .join(ExternalConnection)
            .join(ExternalMember)
            .where(
                ExternalMember.user_id == actor,
                ExternalConversation.connection_id.in_(self.connection_ids),
            )
            .order_by(ExternalConversation.id)
            .limit(1001)
        )
        values = rows.all()
        if len(values) > 1000:
            raise DatabaseBusy()
        return tuple(
            ExternalRoom(
                room.id,
                connection.id,
                Profile(connection.profile),
                room.external_conversation_id,
                room.external_conversation_id,
                room.last_seq,
            )
            for room, connection in values
        )
