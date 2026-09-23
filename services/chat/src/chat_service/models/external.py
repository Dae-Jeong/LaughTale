"""외부 채널 전용 원장입니다. 내부 DM의 사용자·회원 제약을 변경하지 않습니다."""

from datetime import datetime
from uuid import UUID

from platform_contracts.wire import DeliveryState
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from chat_service.models.base import Base, CreatedAtMixin


class ExternalConnection(Base):
    __tablename__ = "external_connections"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    profile: Mapped[str] = mapped_column(Text)
    run_id: Mapped[str] = mapped_column(Text)
    idempotency_supported: Mapped[bool] = mapped_column(Boolean, server_default="true")
    lookup_supported: Mapped[bool] = mapped_column(Boolean, server_default="true")


class ExternalConversation(Base):
    __tablename__ = "external_conversations"
    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "external_conversation_id",
            name="uq_external_conversations_provider",
        ),
        UniqueConstraint(
            "id", "connection_id", name="uq_external_conversations_connection"
        ),
        CheckConstraint("last_seq >= 0", name="ck_external_conversations_seq"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    connection_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("chat.external_connections.id")
    )
    external_conversation_id: Mapped[str] = mapped_column(Text)
    last_seq: Mapped[int] = mapped_column(BigInteger, server_default="0")


class ExternalParticipant(Base):
    __tablename__ = "external_participants"
    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "external_sender_id",
            name="uq_external_participants_provider",
        ),
        UniqueConstraint(
            "id", "connection_id", name="uq_external_participants_connection"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    connection_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("chat.external_connections.id")
    )
    external_sender_id: Mapped[str] = mapped_column(Text)


class ExternalMember(Base):
    __tablename__ = "external_members"

    conversation_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("chat.external_conversations.id"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("chat.users.id"), primary_key=True
    )


class ExternalMessage(CreatedAtMixin, Base):
    __tablename__ = "external_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "connection_id"],
            [
                "chat.external_conversations.id",
                "chat.external_conversations.connection_id",
            ],
            name="fk_external_messages_conversation",
        ),
        ForeignKeyConstraint(
            ["participant_id", "connection_id"],
            [
                "chat.external_participants.id",
                "chat.external_participants.connection_id",
            ],
            name="fk_external_messages_participant",
        ),
        ForeignKeyConstraint(
            ["conversation_id", "operator_user_id"],
            ["chat.external_members.conversation_id", "chat.external_members.user_id"],
            name="fk_external_messages_operator",
        ),
        UniqueConstraint("conversation_id", "seq", name="uq_external_messages_seq"),
        UniqueConstraint(
            "conversation_id",
            "external_message_id",
            name="uq_external_messages_provider",
        ),
        UniqueConstraint(
            "conversation_id",
            "operator_user_id",
            "client_message_id",
            name="uq_external_messages_client",
        ),
        UniqueConstraint("id", "connection_id", name="uq_external_messages_connection"),
        CheckConstraint(
            "(participant_id IS NOT NULL AND operator_user_id IS NULL AND external_message_id IS NOT NULL AND client_message_id IS NULL) OR (participant_id IS NULL AND operator_user_id IS NOT NULL AND external_message_id IS NULL AND client_message_id IS NOT NULL)",
            name="ck_external_messages_sender",
        ),
        CheckConstraint("seq > 0", name="ck_external_messages_seq"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(Uuid)
    connection_id: Mapped[UUID] = mapped_column(Uuid)
    participant_id: Mapped[UUID | None] = mapped_column(Uuid)
    operator_user_id: Mapped[UUID | None] = mapped_column(Uuid)
    client_message_id: Mapped[UUID | None] = mapped_column(Uuid)
    external_message_id: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    seq: Mapped[int] = mapped_column(BigInteger)
    payload_hash: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExternalInboundEvent(CreatedAtMixin, Base):
    __tablename__ = "external_inbound_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["message_id", "connection_id"],
            ["chat.external_messages.id", "chat.external_messages.connection_id"],
            name="fk_external_events_message",
        ),
    )

    connection_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("chat.external_connections.id"), primary_key=True
    )
    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    message_id: Mapped[UUID] = mapped_column(Uuid)
    payload_hash: Mapped[str] = mapped_column(Text)


class ExternalOutboundJob(CreatedAtMixin, Base):
    __tablename__ = "external_outbound_jobs"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'sending', 'accepted', 'rejected', 'unknown')",
            name="ck_external_jobs_state",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_external_jobs_attempts"),
        CheckConstraint(
            "(state = 'sending' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) OR (state <> 'sending' AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_external_jobs_lease",
        ),
        CheckConstraint(
            "state <> 'accepted' OR effect_id IS NOT NULL",
            name="ck_external_jobs_accepted",
        ),
        Index("ix_external_jobs_ready", "state", "next_attempt_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    message_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("chat.external_messages.id"), unique=True
    )
    state: Mapped[str] = mapped_column(Text, server_default=DeliveryState.PENDING.value)
    attempt_count: Mapped[int] = mapped_column(Integer, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None] = mapped_column(Uuid)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effect_id: Mapped[str | None] = mapped_column(Text)
    last_error_code: Mapped[str | None] = mapped_column(Text)
