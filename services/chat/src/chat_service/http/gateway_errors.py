from http import HTTPStatus

from fastapi import Request
from starlette.responses import JSONResponse

from chat_service.contracts.subscriptions import RegistryUnavailable
from chat_service.exceptions.gateway import (
    EventIdentityMismatch,
    InstanceMismatch,
    ResyncUnconfirmed,
)
from chat_service.http.errors import problem_response
from chat_service.schemas.responses import ErrorCode

GATEWAY_ERRORS: dict[type[Exception], tuple[HTTPStatus, ErrorCode]] = {
    InstanceMismatch: (HTTPStatus.CONFLICT, ErrorCode.INSTANCE_MISMATCH),
    EventIdentityMismatch: (
        HTTPStatus.UNPROCESSABLE_CONTENT,
        ErrorCode.EVENT_IDENTITY_MISMATCH,
    ),
    RegistryUnavailable: (
        HTTPStatus.SERVICE_UNAVAILABLE,
        ErrorCode.REGISTRY_UNAVAILABLE,
    ),
    ResyncUnconfirmed: (HTTPStatus.SERVICE_UNAVAILABLE, ErrorCode.RESYNC_NOT_CONFIRMED),
}


async def gateway_error(
    request: Request, exc: Exception, *, status: HTTPStatus, code: ErrorCode
) -> JSONResponse:
    return problem_response(request, status=status, code=code)
