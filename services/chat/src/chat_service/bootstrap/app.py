from functools import partial
from http import HTTPStatus

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware

from chat_service.bootstrap.contracts import PrepareResources
from chat_service.bootstrap.lifespan import (
    create_lifespan,
    prepare_resources,
)
from chat_service.core.chat_hub import ChatHub
from chat_service.core.clock import system_clock
from chat_service.core.contracts import Clock, LogContext
from chat_service.core.database_metrics import create_database_metrics
from chat_service.core.metrics import create_metrics
from chat_service.core.sessions import LocalSessions, LocalSessionStore
from chat_service.core.settings import Settings
from chat_service.exceptions.chat import (
    ConversationNotFoundError,
    IdempotencyConflictError,
    InvalidCursorError,
    InvalidMessageTextError,
    UnauthenticatedError,
)
from chat_service.exceptions.database import DatabaseBusy, DatabasePoolTimeout
from chat_service.http.admission import MessageAdmission
from chat_service.http.database import database_unavailable
from chat_service.http.errors import (
    application_error,
    http_error,
    internal_error,
    problem_openapi,
    validation_error,
)
from chat_service.http.local_security import LocalChatSecurity
from chat_service.routers.chat import router as chat_router
from chat_service.routers.chat_ws import router as chat_ws_router
from chat_service.routers.external import router as external_router
from chat_service.routers.external_ws import router as external_ws_router
from chat_service.routers.health import router as health_router
from chat_service.routers.index import router as index_router
from chat_service.routers.metrics import router as metrics_router
from chat_service.schemas.responses import ErrorCode
from chat_service.transports.external_ws import ConnectionSlots


def create_app(
    settings: Settings,
    *,
    clock: Clock = system_clock,
    prepare: PrepareResources | None = None,
) -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.service_version,
        lifespan=create_lifespan(
            prepare
            if prepare is not None
            else partial(prepare_resources, settings=settings)
        ),
    )
    app.state.clock = clock
    app.state.settings = settings
    app.state.sessions = LocalSessionStore(LocalSessions(clock))
    app.state.ready = False
    app.state.metrics = create_metrics()
    app.state.chat_hub = ChatHub(app.state.metrics)
    app.state.external_slots = ConnectionSlots()
    if settings.db_primary_url:
        app.state.database_metrics = create_database_metrics(
            app.state.metrics, settings.db_pool_size + settings.db_pool_max_overflow
        )
    app.state.log_context = LogContext(
        settings.app_name, settings.service_version, settings.app_environment
    )
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(HTTPException, http_error)
    app.add_exception_handler(Exception, internal_error)
    app.include_router(index_router)
    app.include_router(health_router)
    app.include_router(metrics_router)
    if settings.dev_sessions_enabled:
        app.include_router(chat_router)
        app.include_router(chat_ws_router)
    if settings.external_enabled:
        app.include_router(external_router)
        app.include_router(external_ws_router)
    if settings.lab_message_admission_limit:
        if settings.network_profile != "isolated-lab":
            raise ValueError("Message admission experiment requires isolated lab")
        app.add_middleware(MessageAdmission, limit=settings.lab_message_admission_limit)
    app.add_middleware(LocalChatSecurity, settings=settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.dev_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
        expose_headers=["X-Request-ID"],
    )
    for error, status, code in (
        (UnauthenticatedError, HTTPStatus.UNAUTHORIZED, ErrorCode.UNAUTHENTICATED),
        (
            ConversationNotFoundError,
            HTTPStatus.NOT_FOUND,
            ErrorCode.CONVERSATION_NOT_FOUND,
        ),
        (IdempotencyConflictError, HTTPStatus.CONFLICT, ErrorCode.IDEMPOTENCY_CONFLICT),
        (
            InvalidMessageTextError,
            HTTPStatus.UNPROCESSABLE_ENTITY,
            ErrorCode.INVALID_INPUT,
        ),
        (InvalidCursorError, HTTPStatus.UNPROCESSABLE_ENTITY, ErrorCode.INVALID_INPUT),
    ):
        app.add_exception_handler(
            error, partial(application_error, status=status, code=code)
        )
    if settings.db_primary_url:
        app.add_exception_handler(DatabaseBusy, database_unavailable)
        app.add_exception_handler(DatabasePoolTimeout, database_unavailable)
    # FastAPI가 지원하는 인스턴스별 OpenAPI 함수 교체입니다. self는 partial로 고정합니다.
    app.openapi = partial(problem_openapi, app, app.openapi)  # ty: ignore[invalid-assignment]
    return app
