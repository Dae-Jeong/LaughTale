from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from chat_service.models.base import Base, CreatedAtMixin


class SharedSession(CreatedAtMixin, Base):
    __tablename__ = "shared_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("chat.users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
