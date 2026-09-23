from http import HTTPStatus

from fastapi import Request
from fastapi.responses import JSONResponse

from chat_service.exceptions.database import DatabaseBusy, DatabasePoolTimeout
from chat_service.http.errors import problem_response
from chat_service.schemas.responses import ErrorCode


async def database_unavailable(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, DatabaseBusy):
        code = ErrorCode.DATABASE_BUSY
    elif isinstance(exc, DatabasePoolTimeout):
        code = ErrorCode.DATABASE_POOL_TIMEOUT
    else:
        raise exc
    return problem_response(
        request,
        status=HTTPStatus.SERVICE_UNAVAILABLE,
        code=code,
        headers={"Retry-After": "1"},
    )
