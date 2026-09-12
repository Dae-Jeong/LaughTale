"""자원 수명 (LAUGH-KNOWLEDGE-READ-001).

fixture 적재와 원본·캐시 준비를 lifespan에서 수행하고, 획득 직후 정리를 등록합니다.
부분 초기화 실패·취소에서도 이미 획득한 자원을 정리한 뒤 원래 실패를 전파합니다.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from theme_catalog.core.catalog import build_catalog
from theme_catalog.core.fixture import load_fixture
from theme_catalog.core.settings import Settings

type PrepareResources = Callable[[FastAPI, AsyncExitStack], Awaitable[None]]
# asynccontextmanager는 async iterator가 아니라 context manager factory를 돌려줍니다.
type Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


async def prepare_resources(
    app: FastAPI, stack: AsyncExitStack, *, settings: Settings
) -> None:
    """합성 fixture를 한 번 적재해 앱별 원본·캐시를 준비합니다.

    갱신은 이 프로세스 수명까지만 유지되며 종료 시 함께 사라집니다.
    """
    fixture = load_fixture(settings.fixture_path)
    app.state.fixture = fixture
    stack.callback(delattr, app.state, "fixture")

    # 조립 경계: fixture 적재 결과에서 업무 Theme만 꺼내 core에 전달합니다.
    app.state.catalog = build_catalog(
        fixture.themes,
        cache_enabled=settings.cache_enabled,
        capacity=settings.cache_capacity,
    )
    stack.callback(delattr, app.state, "catalog")


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
