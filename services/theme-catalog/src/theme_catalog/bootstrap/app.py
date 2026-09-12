"""앱 조립 (LAUGH-KNOWLEDGE-READ-001).

`create_app`이 조립 지점입니다. factory/import에서 I/O나 프로세스 logger 변경을
하지 않습니다. 기존 chat_service를 import하지 않는 독립 서비스입니다.
"""

from functools import partial

from fastapi import FastAPI

from theme_catalog.bootstrap.lifespan import (
    PrepareResources,
    create_lifespan,
    prepare_resources,
)
from theme_catalog.core.settings import Settings
from theme_catalog.http.errors import ERROR_HANDLERS
from theme_catalog.routers.health import router as health_router
from theme_catalog.routers.stats import router as stats_router
from theme_catalog.routers.themes import router as themes_router

ROUTERS = (health_router, themes_router, stats_router)


def create_app(
    settings: Settings, *, prepare: PrepareResources | None = None
) -> FastAPI:
    """앱별 설정·자원 수명을 연결합니다. 자원은 lifespan에서 준비합니다."""
    app = FastAPI(
        title=settings.app_name,
        version=settings.service_version,
        lifespan=create_lifespan(
            prepare
            if prepare is not None
            else partial(prepare_resources, settings=settings)
        ),
    )
    app.state.settings = settings
    app.state.ready = False
    for error, handler in ERROR_HANDLERS:
        app.add_exception_handler(error, handler)
    for router in ROUTERS:
        app.include_router(router)
    return app
