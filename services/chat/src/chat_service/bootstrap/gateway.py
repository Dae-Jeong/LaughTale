"""Gateway는 HTTP 쓰기/세션 발급 라우트 없이 별도로 조립합니다."""

from functools import partial
from uuid import uuid4

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from chat_service.bootstrap.gateway_lifespan import create_lifespan
from chat_service.contracts.subscriptions import GatewayTarget
from chat_service.core.clock import system_clock
from chat_service.core.contracts import LogContext
from chat_service.core.database_metrics import create_database_metrics
from chat_service.core.metrics import create_metrics
from chat_service.core.realtime_settings import RealtimeSettings, validate_ip
from chat_service.core.settings import Settings
from chat_service.http.errors import internal_error, problem_openapi, validation_error
from chat_service.http.gateway_errors import GATEWAY_ERRORS, gateway_error
from chat_service.http.gateway_security import GatewaySecurity
from chat_service.routers.chat_ws import router
from chat_service.routers.gateway import router as gateway_router
from chat_service.routers.metrics import router as metrics_router


def create_gateway(settings: Settings, realtime: RealtimeSettings) -> FastAPI:
    validate_ip(realtime.gateway_ip, realtime.realtime_allow_loopback)
    if not settings.db_primary_url:
        raise ValueError("PRIMARY_REQUIRED")

    app = FastAPI(
        title="Chat Connection Gateway",
        lifespan=create_lifespan(settings, realtime),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.clock = system_clock
    app.state.log_context = LogContext(
        settings.app_name, settings.service_version, settings.app_environment
    )
    app.state.ready = False
    app.state.target = GatewayTarget(uuid4(), realtime.gateway_ip)
    app.state.metrics = create_metrics()
    app.state.database_metrics = create_database_metrics(
        app.state.metrics, settings.db_pool_size + settings.db_pool_max_overflow
    )
    app.include_router(router)
    app.include_router(gateway_router)
    app.include_router(metrics_router)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(Exception, internal_error)
    for error, (status, code) in GATEWAY_ERRORS.items():
        app.add_exception_handler(
            error, partial(gateway_error, status=status, code=code)
        )
    app.add_middleware(GatewaySecurity, settings=realtime)
    app.openapi = partial(problem_openapi, app, app.openapi)  # ty: ignore[invalid-assignment]

    return app
