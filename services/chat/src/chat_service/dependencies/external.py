from http import HTTPStatus
from secrets import compare_digest
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from platform_contracts.wire import Profile
from starlette.exceptions import HTTPException

from chat_service.core.settings import ConnectionCredential
from chat_service.dependencies.chat import get_chat_service
from chat_service.services.external import ExternalService


def get_external_service(request: Request) -> ExternalService:
    return ExternalService(
        get_chat_service(request),
        request.app.state.clock,
        connection_ids=frozenset(
            UUID(key)
            for key in request.app.state.settings.external_connection_credentials
        ),
    )


def require_bearer(request: Request, expected: str) -> None:
    authorization = request.headers.getlist("authorization")
    if (
        not expected
        or len(authorization) != 1
        or not authorization[0].isascii()
        or not compare_digest(authorization[0], "Bearer " + expected)
    ):
        raise HTTPException(HTTPStatus.UNAUTHORIZED)


def require_control(request: Request) -> None:
    require_bearer(
        request, request.app.state.settings.external_control_token.get_secret_value()
    )


def get_connection(
    request: Request, connection: UUID, profile: Profile
) -> ConnectionCredential:
    config = request.app.state.settings.external_connection_credentials.get(
        str(connection)
    )
    if config is None or config.profile != profile:
        raise HTTPException(HTTPStatus.FORBIDDEN)
    return config


def require_connection(request: Request, connection: UUID, profile: Profile) -> None:
    config = get_connection(request, connection, profile)
    require_bearer(request, config.token.get_secret_value())


ExternalServiceDep = Annotated[ExternalService, Depends(get_external_service)]
