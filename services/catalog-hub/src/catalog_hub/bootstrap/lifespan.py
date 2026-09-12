"""자원 수명 (Laughtale 캐싱 실험).

엔진·세션 팩토리·응답 캐시·표본 버퍼를 준비하고, 획득 직후 정리를 등록합니다.
부분 초기화 실패·취소에서도 이미 획득한 자원을 정리한 뒤 원래 실패를 전파합니다.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from catalog_hub.core.response_cache import ResponseCache
from catalog_hub.core.settings import Settings

type PrepareResources = Callable[[FastAPI, AsyncExitStack], Awaitable[None]]
type Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


async def prepare_resources(
    app: FastAPI, stack: AsyncExitStack, *, settings: Settings
) -> None:
    """DB 엔진과 앱별 캐시·표본 버퍼를 준비합니다."""
    engine = create_async_engine(
        settings.db_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_pool_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_pre_ping=True,
        connect_args={
            "timeout": settings.db_connect_timeout_seconds,
            "server_settings": {
                "application_name": settings.app_name,
                "statement_timeout": str(settings.db_statement_timeout_ms),
            },
        },
        hide_parameters=True,
    )
    stack.push_async_callback(engine.dispose)
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))

    app.state.engine = engine
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    stack.callback(delattr, app.state, "engine")
    stack.callback(delattr, app.state, "session_factory")

    # Step 0은 캐시 없음이 기준선입니다.
    app.state.response_cache = (
        ResponseCache() if settings.response_cache_enabled else None
    )
    stack.callback(delattr, app.state, "response_cache")

    app.state.stage_samples = []
    stack.callback(delattr, app.state, "stage_samples")


def create_lifespan(prepare: PrepareResources) -> Lifespan:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.ready = False
        stack = AsyncExitStack()
        try:
            await prepare(app, stack)
            app.state.ready = True
            yield
        except BaseException as error:
            # 취소와 초기화 실패도 자원을 정리한 뒤 원래 실패로 전파합니다.
            app.state.ready = False
            try:
                await stack.aclose()
            except BaseException as cleanup_error:
                raise error from cleanup_error
            raise
        else:
            app.state.ready = False
            await stack.aclose()

    return lifespan
