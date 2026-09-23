from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from platform_contracts.wire import AcceptedEffect, DeliveryState, OutboundCommand


class DeliveryError(StrEnum):
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_REJECTED = "PROVIDER_REJECTED"
    UNEXPECTED_RESPONSE = "UNEXPECTED_RESPONSE"
    RESPONSE_UNKNOWN = "RESPONSE_UNKNOWN"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
    DELIVERY_ERROR = "DELIVERY_ERROR"


@dataclass(frozen=True, slots=True)
class DeliveryClaim:
    command: OutboundCommand
    lease_token: UUID
    attempt_count: int
    created_at: datetime
    uncertain: bool
    prior_unknown: bool
    idempotency_supported: bool
    lookup_supported: bool


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    state: DeliveryState
    effect_id: str | None = None
    error_code: DeliveryError | None = None
    retry_after: float = 0


class PlatformPort(Protocol):
    async def send_message(self, command: OutboundCommand) -> DeliveryResult: ...
    async def lookup(self, command: OutboundCommand) -> AcceptedEffect | None: ...
