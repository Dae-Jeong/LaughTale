"""최초 채팅 테이블. 변경 가능한 런타임 모델을 import하지 않습니다."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        schema=schema,
    )
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False, server_default="dm"),
        sa.Column("last_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.CheckConstraint("kind = 'dm'", name="ck_conversations_kind"),
        sa.CheckConstraint("last_seq >= 0", name="ck_conversations_last_seq"),
        schema=schema,
    )
    op.create_table(
        "members",
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey(f"{schema}.conversations.id"),
            primary_key=True,
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey(f"{schema}.users.id"), primary_key=True
        ),
        schema=schema,
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey(f"{schema}.conversations.id"),
            nullable=False,
        ),
        sa.Column("sender_id", sa.Uuid(), nullable=False),
        sa.Column("client_message_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("payload_version", sa.Integer(), nullable=False),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "sender_id"],
            [f"{schema}.members.conversation_id", f"{schema}.members.user_id"],
            name="fk_messages_member",
        ),
        sa.UniqueConstraint(
            "conversation_id", "seq", name="uq_messages_conversation_seq"
        ),
        sa.UniqueConstraint(
            "conversation_id",
            "sender_id",
            "client_message_id",
            name="uq_messages_idempotency",
        ),
        sa.CheckConstraint("seq > 0", name="ck_messages_seq"),
        sa.CheckConstraint(
            "char_length(text) BETWEEN 1 AND 2000", name="ck_messages_text_length"
        ),
        sa.CheckConstraint("payload_version = 1", name="ck_messages_payload_version"),
        sa.CheckConstraint(
            "payload_hash ~ '^[0-9a-f]{64}$'", name="ck_messages_payload_hash"
        ),
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    for name in ("messages", "members", "conversations", "users"):
        op.drop_table(name, schema=schema)
