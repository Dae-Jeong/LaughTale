"""최초 채팅 테이블. 변경 가능한 런타임 모델을 import하지 않습니다."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

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
        schema=schema,
    )

    op.create_table(
        "message_outbox",
        sa.Column(
            "event_id",
            sa.Uuid(),
            sa.ForeignKey(f"{schema}.messages.id"),
            primary_key=True,
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        schema=schema,
    )
    op.create_index(
        "ix_message_outbox_unpublished",
        "message_outbox",
        ["created_at"],
        schema=schema,
        postgresql_where=sa.text("published_at IS NULL"),
    )

    op.create_table(
        "shared_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column(
            "actor_id", sa.Uuid(), sa.ForeignKey(f"{schema}.users.id"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().opts["version_table_schema"]
    op.drop_table("shared_sessions", schema=schema)
    for name in ("message_outbox", "messages", "members", "conversations", "users"):
        op.drop_table(name, schema=schema)
