from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from platform_contracts.wire import DeliveryState, Profile


class SenderKind(StrEnum):
    CUSTOMER = "customer"
    OPERATOR = "operator"


@dataclass(frozen=True, slots=True)
class ExternalRoom:
    conversation_id: UUID
    connection_id: UUID
    profile: Profile
    external_conversation_id: str
    title: str
    head_seq: int


@dataclass(frozen=True, slots=True)
class ExternalMessageValue:
    message_id: UUID
    conversation_id: UUID
    sender_kind: SenderKind
    sender_id: str
    client_message_id: UUID | None
    external_message_id: str | None
    seq: int
    text: str = field(repr=False)
    occurred_at: datetime
    received_at: datetime
    created_at: datetime
    operation_id: UUID | None
    delivery_state: DeliveryState | None
    external_sender_id: str | None
    effect_id: str | None


@dataclass(frozen=True, slots=True)
class ExternalStored:
    message: ExternalMessageValue
    replay: bool


@dataclass(frozen=True, slots=True)
class ExternalHistory:
    messages: tuple[ExternalMessageValue, ...]
    next_cursor: int
    snapshot_head_seq: int
    has_more: bool
