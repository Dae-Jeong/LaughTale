"""HTTP 응답 계약 (Laughtale 캐싱 실험).

Backend 응답 계약의 Success/Problem 형태를 최소로 구현합니다.
기존 chat_service·theme_catalog를 runtime import하지 않습니다.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Success[T](BaseModel):
    model_config = ConfigDict(frozen=True)
    data: T


class ErrorCode(StrEnum):
    RELEASE_NOT_FOUND = "RELEASE_NOT_FOUND"
    INVALID_INPUT = "INVALID_INPUT"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    HTTP_ERROR = "HTTP_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class FieldErrorCode(StrEnum):
    REQUIRED = "REQUIRED"
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
    errors: list[FieldError] | None = None
