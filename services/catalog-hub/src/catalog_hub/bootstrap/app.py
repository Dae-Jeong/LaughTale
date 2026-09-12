"""앱 조립 (Laughtale 캐싱 실험).

`create_app`이 조립 지점입니다. factory/import에서 I/O를 하지 않습니다.
기존 chat_service·theme_catalog를 import하지 않는 독립 서비스입니다.
"""

import os
import socket
from functools import partial

from fastapi import FastAPI

from catalog_hub.bootstrap.lifespan import (
    PrepareResources,
    create_lifespan,
    prepare_resources,
)
from catalog_hub.core.settings import Settings
from catalog_hub.http.errors import ERROR_HANDLERS
from catalog_hub.routers.catalog import router as catalog_router
from catalog_hub.routers.health import router as health_router
from catalog_hub.routers.stats import router as stats_router

ROUTERS = (health_router, catalog_router, stats_router)


def create_app(
    settings: Settings, *, prepare: PrepareResources | None = None
) -> FastAPI:
    """앱별 설정·자원 수명을 연결합니다."""
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
    # 어느 인스턴스가 응답했는지 표시합니다. K8s에서는 Pod 이름이 들어옵니다.
    app.state.instance_id = os.environ.get("INSTANCE_ID") or socket.gethostname()
    for error, handler in ERROR_HANDLERS:
        app.add_exception_handler(error, handler)
    for router in ROUTERS:
        app.include_router(router)
    return app
