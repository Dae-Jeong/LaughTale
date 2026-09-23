from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from chat_service.contracts.chat import Actor, ChatMessage, ConversationKind, Room
from chat_service.contracts.events import message_created
from chat_service.domain.chat import MessagePayload, payload_fingerprint
from chat_service.exceptions.chat import ConversationNotFoundError
from chat_service.exceptions.database import DatabaseBusy
from chat_service.models.chat import Conversation, Member, Message, MessageOutbox, User

OUTBOX_ROW_LIMIT = 10_000


def message_value(row: Message) -> ChatMessage:
    if row.payload_version != 1:
        raise RuntimeError("Unsupported stored message version")
    return ChatMessage(
        row.id,
        row.conversation_id,
        row.sender_id,
        row.client_message_id,
        row.seq,
        row.text,
        row.created_at,
    )


class ChatRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def authorized_head(
        self, actor: UUID, room: UUID, *, lock: bool = False
    ) -> int:
        query = (
            select(Conversation)
            .join(Member)
            .where(
                Conversation.id == room,
                Member.user_id == actor,
                Conversation.kind == ConversationKind.DM.value,
            )
        )
        if lock:
            query = query.with_for_update(of=Conversation)
        conversation = await self.session.scalar(query)
        if conversation is None or conversation.kind != ConversationKind.DM.value:
            raise ConversationNotFoundError()
        return conversation.last_seq

    async def find_message(
        self, actor: UUID, room: UUID, key: UUID
    ) -> ChatMessage | None:
        row = await self.session.scalar(
            select(Message).where(
                Message.conversation_id == room,
                Message.sender_id == actor,
                Message.client_message_id == key,
            )
        )
        return message_value(row) if row is not None else None

    async def append(
        self, actor: UUID, room: UUID, key: UUID, payload: MessagePayload, seq: int
    ) -> ChatMessage:
        conversation = await self.session.get(Conversation, room)
        if conversation is None or conversation.kind != ConversationKind.DM.value:
            raise ConversationNotFoundError()
        conversation.last_seq = seq
        # 미발행 원장의 로컬 저장 예산입니다. 전역 count/lock 경합은 실험용 제약입니다.
        # 방 잠금 뒤 항상 같은 순서로 획득하며 transaction 종료 시 해제합니다.
        await self.session.execute(text("SELECT pg_advisory_xact_lock(181832, 1)"))
        pending = (
            await self.session.execute(
                select(func.count())
                .select_from(MessageOutbox)
                .where(MessageOutbox.published_at.is_(None))
            )
        ).scalar_one()
        if pending >= OUTBOX_ROW_LIMIT:
            raise DatabaseBusy()
        row = Message(
            id=uuid4(),
            conversation_id=room,
            sender_id=actor,
            client_message_id=key,
            seq=seq,
            text=payload.text,
            payload_version=payload.version,
            payload_hash=payload_fingerprint(payload),
        )
        self.session.add(row)
        await self.session.flush()
        message = message_value(row)
        self.session.add(
            MessageOutbox(event_id=row.id, payload=message_created(message))
        )
        await self.session.flush()
        return message

    async def history(
        self, room: UUID, after: int, head: int, limit: int
    ) -> tuple[ChatMessage, ...]:
        rows = await self.session.scalars(
            select(Message)
            .where(
                Message.conversation_id == room,
                Message.seq > after,
                Message.seq <= head,
            )
            .order_by(Message.seq)
            .limit(limit)
        )
        return tuple(message_value(row) for row in rows)

    async def rooms(self, actor: UUID) -> tuple[Room, ...]:
        conversations = (
            await self.session.scalars(
                select(Conversation)
                .join(Member)
                .where(
                    Member.user_id == actor,
                    Conversation.kind == ConversationKind.DM.value,
                )
                .order_by(Conversation.id)
            )
        ).all()
        result = []
        for room in conversations:
            names = (
                await self.session.scalars(
                    select(User.display_name)
                    .join(Member)
                    .where(Member.conversation_id == room.id, Member.user_id != actor)
                    .order_by(User.id)
                )
            ).all()
            result.append(Room(room.id, ", ".join(names) or "DM", room.last_seq))
        return tuple(result)

    async def actor(self, user: UUID) -> Actor | None:
        row = await self.session.get(User, user)
        return Actor(row.id, row.display_name) if row is not None else None

    async def heads(
        self, actors: set[UUID], rooms: set[UUID]
    ) -> dict[tuple[UUID, UUID], int]:
        rows = await self.session.execute(
            select(Member.user_id, Conversation.id, Conversation.last_seq)
            .join(Member)
            .where(
                Conversation.id.in_(rooms),
                Member.user_id.in_(actors),
                Conversation.kind == ConversationKind.DM.value,
            )
        )
        return {(actor, room): head for actor, room, head in rows}
