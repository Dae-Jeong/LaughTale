"""채팅 영속 매핑입니다. 업무 규칙과 트랜잭션은 실행하지 않습니다."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)

metadata = MetaData(schema="chat")
users = Table(
    "users",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("display_name", Text, nullable=False),
)
conversations = Table(
    "conversations",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("kind", Text, nullable=False, server_default="dm"),
    Column("last_seq", BigInteger, nullable=False, server_default="0"),
    CheckConstraint("kind = 'dm'", name="ck_conversations_kind"),
    CheckConstraint("last_seq >= 0", name="ck_conversations_last_seq"),
)
members = Table(
    "members",
    metadata,
    Column(
        "conversation_id", Uuid, ForeignKey("chat.conversations.id"), primary_key=True
    ),
    Column("user_id", Uuid, ForeignKey("chat.users.id"), primary_key=True),
)
messages = Table(
    "messages",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column(
        "conversation_id", Uuid, ForeignKey("chat.conversations.id"), nullable=False
    ),
    Column("sender_id", Uuid, nullable=False),
    Column("client_message_id", Uuid, nullable=False),
    Column("seq", BigInteger, nullable=False),
    Column("text", Text, nullable=False),
    Column("payload_version", Integer, nullable=False),
    Column("payload_hash", Text, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    ),
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
    CheckConstraint("payload_hash ~ '^[0-9a-f]{64}$'", name="ck_messages_payload_hash"),
)
