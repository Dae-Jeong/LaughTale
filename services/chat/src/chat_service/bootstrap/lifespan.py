import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from uuid import UUID

import httpx2
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from chat_service.adapters.mock_platform import MockPlatform
from chat_service.bootstrap.contracts import Lifespan, PrepareResources
from chat_service.core.database import create_primary_engine
from chat_service.core.settings import Settings
from chat_service.core.shared_sessions import SharedSessions
from chat_service.dependencies.chat import create_service
from chat_service.services.external_delivery import ExternalDelivery


async def prepare_resources(
    app: FastAPI, stack: AsyncExitStack, *, settings: Settings
) -> None:
    if not settings.db_primary_url:
        return
    engine = create_primary_engine(settings, app.state.database_metrics)
    stack.push_async_callback(engine.dispose)
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    app.state.primary_engine = engine
    app.state.primary_session_factory = async_sessionmaker(
        engine, expire_on_commit=False
    )
    stack.callback(delattr, app.state, "primary_engine")
    stack.callback(delattr, app.state, "primary_session_factory")


def create_lifespan(prepare: PrepareResources) -> Lifespan:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.ready = False
        stack = AsyncExitStack()
        try:
            await prepare(app, stack)
            if app.state.settings.session_backend == "postgres":
                app.state.sessions = SharedSessions(
                    app.state.primary_session_factory,
                    metrics=app.state.database_metrics,
                )
            if app.state.settings.dev_sessions_enabled and hasattr(
                app.state, "primary_session_factory"
            ):
                service = create_service(app)
                reconciliation = asyncio.create_task(
                    app.state.chat_hub.reconcile_loop(service, app.state.sessions)
                )

                async def stop_reconciliation() -> None:
                    reconciliation.cancel()
                    await asyncio.gather(reconciliation, return_exceptions=True)

                stack.push_async_callback(stop_reconciliation)
            if app.state.settings.external_enabled and hasattr(
                app.state, "primary_session_factory"
            ):
                settings = app.state.settings
                client = await stack.enter_async_context(
                    httpx2.AsyncClient(
                        base_url=settings.mock_api_url,
                        headers={
                            "Authorization": "Bearer "
                            + settings.mock_api_token.get_secret_value()
                        },
                        timeout=2,
                        follow_redirects=False,
                        trust_env=False,
                        limits=httpx2.Limits(
                            max_connections=1, max_keepalive_connections=1
                        ),
                    )
                )
                delivery = ExternalDelivery(
                    create_service(app),
                    MockPlatform(client),
                    app.state.clock,
                    connection_ids=frozenset(
                        UUID(key) for key in settings.external_connection_credentials
                    ),
                )
                worker = asyncio.create_task(delivery.run())

                async def stop_delivery() -> None:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)

                stack.push_async_callback(stop_delivery)
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
