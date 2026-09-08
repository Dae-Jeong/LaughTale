from datetime import datetime

from sqlalchemy import DateTime, MetaData, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """서비스 영속 모델의 metadata만 공유하며 컬럼을 강제하지 않습니다."""

    metadata = MetaData(schema="chat")


class CreatedAtMixin:
    """생성 시각이 필요한 모델만 명시적으로 사용합니다."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
