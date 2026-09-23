import asyncio
from collections.abc import AsyncIterator, Coroutine
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from chat_service.adapters.redis_registry import RedisRegistry
from chat_service.bootstrap.contracts import Lifespan
from chat_service.bootstrap.lifespan import prepare_resources
from chat_service.core.gateway_hub import GatewayHub
from chat_service.core.realtime_settings import RealtimeSettings
from chat_service.core.settings import Settings
from chat_service.core.shared_sessions import SharedSessions
from chat_service.dependencies.chat import create_service


def create_lifespan(settings: Settings, realtime: RealtimeSettings) -> Lifespan:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.ready = False
        stack = AsyncExitStack()
        try:
            await prepare_resources(app, stack, settings=settings)
            app.state.sessions = SharedSessions(app.state.primary_session_factory)
            redis = Redis.from_url(
                realtime.redis_url.get_secret_value(),
                decode_responses=True,
                socket_timeout=1,
                socket_connect_timeout=1,
                max_connections=4,
            )
            stack.push_async_callback(redis.aclose)
            registry = RedisRegistry(
                redis, allow_loopback=realtime.realtime_allow_loopback
            )
            hub = GatewayHub(app.state.metrics, registry, app.state.target)
            app.state.chat_hub = hub

            async def stop_hub() -> None:
                try:
                    await hub.shutdown()
                except Exception:
                    pass

            stack.push_async_callback(stop_hub)
            await hub.refresh_locked()
            service = create_service(app)
            tasks: list[asyncio.Task[None]] = []

            async def stop_tasks() -> None:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

            stack.push_async_callback(stop_tasks)

            def start(coro: Coroutine[object, object, None]) -> None:
                try:
                    task = asyncio.create_task(coro)
                except BaseException:
                    coro.close()
                    raise
                tasks.append(task)

            start(hub.maintain(service, app.state.sessions))
            start(hub.watch_expiry())
            app.state.ready = True
            yield
        finally:
            app.state.ready = False
            await stack.aclose()

    return lifespan
