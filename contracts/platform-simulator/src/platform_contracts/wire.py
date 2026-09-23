"""Chat과 mock의 유일한 Python wire 정의입니다. 업무·DB 의존성은 없습니다."""

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field


class Profile(StrEnum):
    TELEGRAM = "telegram"
    LINE = "line"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    WHATSAPP = "whatsapp"
    WECHAT = "wechat"
    KAKAO = "kakao-bizgo"


class Fault(StrEnum):
    NONE = "none"
    RATE_LIMIT = "rate_limit"
    UNAVAILABLE = "unavailable"
    DELAY_BEFORE = "delay_before"
    DELAY_AFTER = "delay_after"
    REJECT = "reject"


class DeliveryState(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


def valid_text(value: str) -> str:
    if not value.strip() or "\x00" in value:
        raise ValueError("Invalid message text")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ValueError("Invalid Unicode")
    return value


Identifier = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
]
Text = Annotated[str, Field(min_length=1, max_length=2000), AfterValidator(valid_text)]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class InboundEvent(WireModel):
    schema_version: Literal[1] = 1
    run_id: Identifier
    profile: Profile
    connection_id: UUID
    external_conversation_id: Identifier
    external_event_id: Identifier
    external_message_id: Identifier
    external_sender_id: Identifier
    occurred_at: AwareDatetime
    text: Text = Field(repr=False)


class OutboundCommand(WireModel):
    schema_version: Literal[1] = 1
    run_id: Identifier
    profile: Profile
    connection_id: UUID
    external_conversation_id: Identifier
    outbound_operation_id: UUID
    text: Text = Field(repr=False)


class AcceptedEffect(WireModel):
    effect_id: UUID
    outbound_operation_id: UUID
    state: Literal["accepted"] = "accepted"


class RunConfig(WireModel):
    run_id: Identifier
    seed: int = Field(default=0, ge=0, le=2**31 - 1)
    idempotency_supported: bool = True
    lookup_supported: bool = True
    fault: Fault = Fault.NONE
    fault_attempts: int = Field(default=1, ge=0, le=5)
    delay_seconds: float = Field(default=3, ge=0, le=10)
    max_events: int = Field(default=1000, ge=1, le=10000)
    max_attempts: int = Field(default=5000, ge=1, le=50000)
    max_effects: int = Field(default=1000, ge=1, le=10000)


class ConnectionSeed(WireModel):
    """합성 연결의 명시적 준비 입력입니다."""

    run_id: Identifier
    profile: Profile
    connection_id: UUID
    external_conversation_id: Identifier
    external_sender_id: Identifier
    operator_user_id: UUID
    idempotency_supported: bool = True
    lookup_supported: bool = True
