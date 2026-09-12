"""공개 오류 변환 (Laughtale 캐싱 실험).

입력값·내부 메시지를 반환하지 않고 승인한 공개 위치와 고정 코드만 제공합니다.
"""

from http import HTTPStatus

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from catalog_hub.repository.catalog import ReleaseNotFoundError
from catalog_hub.schemas.responses import (
    ErrorCode,
    FieldError,
    FieldErrorCode,
    Problem,
)

MAX_REPORTED_FIELD_ERRORS = 20

# 승인한 공개 위치만 노출합니다.
PUBLIC_LOCATIONS = frozenset({("query", "lang")})


def problem_response(problem: Problem) -> JSONResponse:
    return JSONResponse(status_code=problem.status, content=problem.model_dump())


def public_location(raw_location: tuple[object, ...]) -> list[str]:
    location = tuple(str(part) for part in raw_location)
    if len(location) >= 2 and (location[0], location[-1]) in PUBLIC_LOCATIONS:
        return [location[0], location[-1]]
    return []


async def validation_error(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, RequestValidationError)
    errors = [
        FieldError(
            location=public_location(tuple(detail.get("loc", ()))),
            code=FieldErrorCode.INVALID,
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


async def release_not_found(request: Request, error: Exception) -> JSONResponse:
    """published release가 없습니다. 빈 DB이거나 발행 전입니다."""
    assert isinstance(error, ReleaseNotFoundError)
    return problem_response(
        Problem(
            title="No published release",
            status=HTTPStatus.NOT_FOUND,
            code=ErrorCode.RELEASE_NOT_FOUND,
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
    """500은 고정 INTERNAL_ERROR입니다."""
    return problem_response(
        Problem(
            title="Internal server error",
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            code=ErrorCode.INTERNAL_ERROR,
        )
    )


ERROR_HANDLERS = (
    (RequestValidationError, validation_error),
    (ReleaseNotFoundError, release_not_found),
    (HTTPException, http_error),
    (Exception, internal_error),
)
