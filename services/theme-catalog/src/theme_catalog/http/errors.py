"""공개 오류 변환 (LAUGH-KNOWLEDGE-READ-001).

입력값·내부 메시지는 반환하지 않고 승인한 공개 위치와 고정 코드만 제공합니다.
업무 예외를 HTTP 상태로 바꾸는 책임은 이 경계가 소유합니다.
"""

from http import HTTPStatus

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from theme_catalog.core.store import EmptyUpdateError, ThemeNotFoundError
from theme_catalog.core.theme import InvalidThemeValueError
from theme_catalog.schemas.responses import (
    ErrorCode,
    FieldError,
    FieldErrorCode,
    Problem,
)

MAX_REPORTED_FIELD_ERRORS = 20

# 승인한 공개 위치만 노출합니다. 새 입력을 추가할 때 함께 확장합니다.
PUBLIC_LOCATIONS = frozenset(
    {
        ("path", "theme_id"),
        ("body", "background"),
        ("body", "foreground"),
        ("body", "spacing_px"),
    }
)

FIELD_ERROR_CODES = {
    "missing": FieldErrorCode.REQUIRED,
    "string_too_short": FieldErrorCode.TOO_SHORT,
    "string_too_long": FieldErrorCode.TOO_LONG,
}


class EmptyPatchError(Exception):
    """바꿀 속성이 하나도 없는 PATCH입니다. 업무 실행 전에 거절합니다."""


def problem_response(problem: Problem) -> JSONResponse:
    return JSONResponse(status_code=problem.status, content=problem.model_dump())


def public_location(raw_location: tuple[object, ...]) -> list[str]:
    """알 수 없는 위치는 빈 배열로 축약합니다."""
    location = tuple(str(part) for part in raw_location)
    if len(location) >= 2 and (location[0], location[-1]) in PUBLIC_LOCATIONS:
        return [location[0], location[-1]]
    return []


async def validation_error(request: Request, error: Exception) -> JSONResponse:
    """422입니다. input·context·msg는 반환하지 않습니다."""
    assert isinstance(error, RequestValidationError)
    errors = [
        FieldError(
            location=public_location(tuple(detail.get("loc", ()))),
            code=FIELD_ERROR_CODES.get(
                str(detail.get("type", "")), FieldErrorCode.INVALID
            ),
        )
        for detail in error.errors()[:MAX_REPORTED_FIELD_ERRORS]
    ]
    return problem_response(
        Problem(
            title="Invalid request",
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
            code=ErrorCode.INVALID_INPUT,
            errors=errors,
        )
    )


async def empty_patch(request: Request, error: Exception) -> JSONResponse:
    """빈 PATCH도 입력 오류이므로 검증 실패와 같은 코드로 답합니다."""
    return problem_response(
        Problem(
            title="Invalid request",
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
            code=ErrorCode.INVALID_INPUT,
            errors=[],
        )
    )


async def invalid_theme_value(request: Request, error: Exception) -> JSONResponse:
    """원본 값 규칙 위반입니다.

    HTTP schema가 같은 규칙을 먼저 적용하므로 정상 경로에서는 도달하지 않지만,
    두 계약이 갈라져도 500이 아니라 입력 오류로 보이도록 명시적으로 매핑합니다.
    """
    assert isinstance(error, InvalidThemeValueError)
    return problem_response(
        Problem(
            title="Invalid request",
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
            code=ErrorCode.INVALID_INPUT,
            errors=[],
        )
    )


async def theme_not_found(request: Request, error: Exception) -> JSONResponse:
    """미등록 theme_id입니다. 명시적 404이며 캐시에 남기지 않습니다."""
    assert isinstance(error, ThemeNotFoundError)
    return problem_response(
        Problem(
            title="Theme not found",
            status=HTTPStatus.NOT_FOUND,
            code=ErrorCode.THEME_NOT_FOUND,
        )
    )


async def http_error(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, HTTPException)
    status = HTTPStatus(error.status_code)
    code = {
        HTTPStatus.NOT_FOUND: ErrorCode.NOT_FOUND,
        HTTPStatus.METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED,
    }.get(status, ErrorCode.HTTP_ERROR)
    return problem_response(Problem(title=status.phrase, status=status, code=code))


async def internal_error(request: Request, error: Exception) -> JSONResponse:
    """500은 고정 INTERNAL_ERROR입니다. 내부 상세를 노출하지 않습니다."""
    return problem_response(
        Problem(
            title="Internal server error",
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            code=ErrorCode.INTERNAL_ERROR,
        )
    )


# 업무 예외 → HTTP 변환 등록표입니다. 조립 지점이 그대로 순회합니다.
ERROR_HANDLERS = (
    (RequestValidationError, validation_error),
    (EmptyPatchError, empty_patch),
    (EmptyUpdateError, empty_patch),
    (InvalidThemeValueError, invalid_theme_value),
    (ThemeNotFoundError, theme_not_found),
    (HTTPException, http_error),
    (Exception, internal_error),
)
