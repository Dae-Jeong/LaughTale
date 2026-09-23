from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Success[T](BaseModel):
    model_config = ConfigDict(frozen=True)
    data: T


class MessageData(BaseModel):
    message: str


class ErrorCode(StrEnum):
    UNAUTHENTICATED = "UNAUTHENTICATED"
    CONVERSATION_NOT_FOUND = "CONVERSATION_NOT_FOUND"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INVALID_INPUT = "INVALID_INPUT"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    HTTP_ERROR = "HTTP_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    DATABASE_BUSY = "DATABASE_BUSY"
    DATABASE_POOL_TIMEOUT = "DATABASE_POOL_TIMEOUT"
    INSTANCE_MISMATCH = "INSTANCE_MISMATCH"
    EVENT_IDENTITY_MISMATCH = "EVENT_IDENTITY_MISMATCH"
    REGISTRY_UNAVAILABLE = "REGISTRY_UNAVAILABLE"
    RESYNC_NOT_CONFIRMED = "RESYNC_NOT_CONFIRMED"


class FieldErrorCode(StrEnum):
    REQUIRED = "REQUIRED"
    TOO_SHORT = "TOO_SHORT"
    TOO_LONG = "TOO_LONG"
    INVALID = "INVALID"


class FieldError(BaseModel):
    location: list[str]
    code: FieldErrorCode


class Problem(BaseModel):
    model_config = ConfigDict(frozen=True)
    type: Literal["about:blank"] = "about:blank"
    title: str
    status: int = Field(ge=400, le=599)
    code: ErrorCode
    request_id: str
    errors: list[FieldError] | None = None
