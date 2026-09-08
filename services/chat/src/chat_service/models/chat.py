"""채팅 ORM 매핑입니다. 업무 정책·트랜잭션과 자동 관계 로딩은 포함하지 않습니다."""

from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from chat_service.models.base import Base, CreatedAtMixin


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint("kind = 'dm'", name="ck_conversations_kind"),
        CheckConstraint("last_seq >= 0", name="ck_conversations_last_seq"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="dm")
    last_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )


class Member(Base):
    __tablename__ = "members"

    conversation_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("chat.conversations.id"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("chat.users.id"), primary_key=True
    )


class Message(CreatedAtMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "sender_id"],
            ["chat.members.conversation_id", "chat.members.user_id"],
            name="fk_messages_member",
        ),
        UniqueConstraint("conversation_id", "seq", name="uq_messages_conversation_seq"),
        UniqueConstraint(
            "conversation_id",
            "sender_id",
            "client_message_id",
            name="uq_messages_idempotency",
        ),
        CheckConstraint("seq > 0", name="ck_messages_seq"),
        CheckConstraint(
            "char_length(text) BETWEEN 1 AND 2000", name="ck_messages_text_length"
        ),
        CheckConstraint("payload_version = 1", name="ck_messages_payload_version"),
        CheckConstraint(
            "payload_hash ~ '^[0-9a-f]{64}$'", name="ck_messages_payload_hash"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("chat.conversations.id"), nullable=False
    )
    sender_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    client_message_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    payload_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_hash: Mapped[str] = mapped_column(Text, nullable=False)


# 기존 Alembic·제약 시험이 소비하는 동일 metadata이며 테이블을 이중 정의하지 않습니다.
metadata = Base.metadata
