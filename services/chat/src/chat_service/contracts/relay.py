"""Outbox 발행의 어댑터 경계입니다."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class RelayOutcome(StrEnum):
    IDLE = "idle"
    RETRY = "retry"
    PUBLISHED = "published"
    STALE = "stale"
    DATABASE_RETRY = "database_retry"
    UNKNOWN = "unknown"


class RelayError(StrEnum):
    PUBLISH_FAILED = "PUBLISH_FAILED"


class PublisherShutdownError(Exception):
    """종료 불명확한 publisher는 프로세스 재시작으로 복구합니다."""


@dataclass(frozen=True)
class RelayClaim:
    event_id: UUID
    conversation_id: UUID
    token: UUID
    attempt: int
    payload: dict


class EventPublisher(Protocol):
    async def publish(self, claim: RelayClaim) -> None:
        """Broker ACK가 확인된 경우에만 반환합니다."""
        ...
