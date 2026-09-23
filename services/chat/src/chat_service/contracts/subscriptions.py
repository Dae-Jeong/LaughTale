"""구독 위치는 재구성 가능한 라우팅 정보이며 메시지 원본이 아닙니다."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class GatewayOutcome(StrEnum):
    ACCEPTED = "accepted"
    NO_SUBSCRIBERS = "no_subscribers"
    RESYNC_REQUIRED = "resync_required"


class ResyncReason(StrEnum):
    QUEUE_OVERFLOW = "queue_overflow"
    RECOVERY_UNAVAILABLE = "recovery_unavailable"


class ReconcileOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class GatewayTarget:
    instance_id: UUID
    ip: str


class SubscriptionRegistry(Protocol):
    async def sync(self, target: GatewayTarget, rooms: set[UUID]) -> None: ...
    async def remove(self, target: GatewayTarget) -> None: ...
    async def targets(self, room: UUID) -> list[GatewayTarget]: ...


class RegistryUnavailable(Exception):
    pass
