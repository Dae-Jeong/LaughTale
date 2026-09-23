import asyncio
from contextlib import AsyncExitStack
from uuid import uuid4

import pytest
from fastapi import FastAPI

from chat_service.bootstrap import gateway_lifespan
from chat_service.contracts.subscriptions import GatewayTarget
from chat_service.core.realtime_settings import RealtimeSettings
from chat_service.core.settings import Settings


class Redis:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    @classmethod
    def from_url(cls, *args, **kwargs):
        return cls(kwargs.pop("events"))

    async def aclose(self) -> None:
        self.events.append("redis.close")


class Hub:
    def __init__(self, events: list[str], fail_sync: bool = False) -> None:
        self.events = events
        self.fail_sync = fail_sync

    async def refresh_locked(self) -> None:
        self.events.append("hub.sync")
        if self.fail_sync:
            raise RuntimeError("sync failed")

    async def maintain(self, service, sessions) -> None:
        await asyncio.Future()

    async def watch_expiry(self) -> None:
        await asyncio.Future()

    async def shutdown(self) -> None:
        self.events.append("hub.shutdown")


class Task:
    def __init__(self, events: list[str], name: str) -> None:
        self.events = events
        self.name = name
        self.done = asyncio.get_running_loop().create_future()

    def cancel(self) -> None:
        self.events.append(f"{self.name}.cancel")
        self.done.cancel()

    def __await__(self):
        return self.done.__await__()


def realtime() -> RealtimeSettings:
    return RealtimeSettings(
        _env_file=None,
        app_environment="isolated-lab",
        network_profile="isolated-lab",
        redis_url="redis://default:test-secret@127.0.0.1:6379/0",
        gateway_delivery_token="test-secret-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        gateway_ip="127.0.0.1",
        realtime_allow_loopback=True,
    )


def settings() -> Settings:
    return Settings(
        _env_file=None, db_primary_url="postgresql+asyncpg://test:test@db/test"
    )


def app() -> FastAPI:
    value = FastAPI()
    value.state.metrics = object()
    value.state.target = GatewayTarget(uuid4(), "127.0.0.1")
    value.state.database_metrics = object()
    value.state.ready = False
    return value


def install_fakes(
    monkeypatch: pytest.MonkeyPatch, events: list[str], *, fail_sync: bool = False
) -> None:
    async def prepare(
        value: FastAPI, stack: AsyncExitStack, *, settings: Settings
    ) -> None:
        events.append("prepare")
        value.state.primary_session_factory = object()

        async def close_database() -> None:
            events.append("database.close")

        stack.push_async_callback(close_database)

    class FakeRedis(Redis):
        @classmethod
        def from_url(cls, *args, **kwargs):
            return cls(events)

    class FakeHub(Hub):
        def __init__(self, metrics, registry, target) -> None:
            super().__init__(events, fail_sync)

    monkeypatch.setattr(gateway_lifespan, "prepare_resources", prepare)
    monkeypatch.setattr(gateway_lifespan, "Redis", FakeRedis)
    monkeypatch.setattr(
        gateway_lifespan, "RedisRegistry", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(gateway_lifespan, "GatewayHub", FakeHub)


def test_gateway_lifespan_starts_ready_and_releases_in_reverse_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        events: list[str] = []
        install_fakes(monkeypatch, events)
        created = 0

        def create_task(coro):
            nonlocal created
            coro.close()
            created += 1
            return Task(events, "maintain" if created == 1 else "expiry")

        monkeypatch.setattr(gateway_lifespan.asyncio, "create_task", create_task)
        value = app()
        async with gateway_lifespan.create_lifespan(settings(), realtime())(value):
            assert value.state.ready is True
        assert value.state.ready is False
        assert events == [
            "prepare",
            "hub.sync",
            "maintain.cancel",
            "expiry.cancel",
            "hub.shutdown",
            "redis.close",
            "database.close",
        ]

    asyncio.run(scenario())


def test_gateway_lifespan_cleans_hub_when_initial_sync_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        events: list[str] = []
        install_fakes(monkeypatch, events, fail_sync=True)
        value = app()
        with pytest.raises(RuntimeError, match="sync failed"):
            async with gateway_lifespan.create_lifespan(settings(), realtime())(value):
                pytest.fail("startup must fail")
        assert value.state.ready is False
        assert events == [
            "prepare",
            "hub.sync",
            "hub.shutdown",
            "redis.close",
            "database.close",
        ]

    asyncio.run(scenario())


def test_cleanup_failure_still_attempts_database_close(monkeypatch) -> None:
    async def scenario() -> None:
        events: list[str] = []
        install_fakes(monkeypatch, events, fail_sync=True)

        async def fail_close(self) -> None:
            events.append("redis.close")
            raise OSError("synthetic cleanup failure")

        monkeypatch.setattr(Redis, "aclose", fail_close)
        value = app()
        with pytest.raises(OSError, match="synthetic cleanup failure"):
            async with gateway_lifespan.create_lifespan(settings(), realtime())(value):
                pytest.fail("startup must fail")
        assert events[-2:] == ["redis.close", "database.close"]
        assert not value.state.ready

    asyncio.run(scenario())


def test_gateway_lifespan_cleans_partial_database_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        events: list[str] = []

        async def prepare(
            value: FastAPI, stack: AsyncExitStack, *, settings: Settings
        ) -> None:
            async def close_database() -> None:
                events.append("database.close")

            stack.push_async_callback(close_database)
            raise RuntimeError("database preparation failed")

        monkeypatch.setattr(gateway_lifespan, "prepare_resources", prepare)
        value = app()
        with pytest.raises(RuntimeError, match="database preparation failed"):
            async with gateway_lifespan.create_lifespan(settings(), realtime())(value):
                pytest.fail("startup must fail")
        assert value.state.ready is False
        assert events == ["database.close"]

    asyncio.run(scenario())


def test_gateway_lifespan_cleans_database_when_redis_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        events: list[str] = []
        install_fakes(monkeypatch, events)

        class FailingRedis:
            @classmethod
            def from_url(cls, *args, **kwargs):
                raise RuntimeError("redis creation failed")

        monkeypatch.setattr(gateway_lifespan, "Redis", FailingRedis)
        value = app()
        with pytest.raises(RuntimeError, match="redis creation failed"):
            async with gateway_lifespan.create_lifespan(settings(), realtime())(value):
                pytest.fail("startup must fail")
        assert value.state.ready is False
        assert events == ["prepare", "database.close"]

    asyncio.run(scenario())


def test_gateway_lifespan_cancellation_during_initial_sync_cleans_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        events: list[str] = []
        started = asyncio.Event()
        install_fakes(monkeypatch, events)

        class BlockingHub(Hub):
            def __init__(self, metrics, registry, target) -> None:
                super().__init__(events)

            async def refresh_locked(self) -> None:
                events.append("hub.sync")
                started.set()
                await asyncio.Future()

        monkeypatch.setattr(gateway_lifespan, "GatewayHub", BlockingHub)
        value = app()

        async def run_lifespan() -> None:
            async with gateway_lifespan.create_lifespan(settings(), realtime())(value):
                pytest.fail("startup must not finish")

        task = asyncio.create_task(run_lifespan())
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert value.state.ready is False
        assert events == [
            "prepare",
            "hub.sync",
            "hub.shutdown",
            "redis.close",
            "database.close",
        ]

    asyncio.run(scenario())


def test_gateway_lifespan_cancellation_releases_all_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        events: list[str] = []
        install_fakes(monkeypatch, events)
        created = 0

        def create_task(coro):
            nonlocal created
            coro.close()
            created += 1
            return Task(events, "maintain" if created == 1 else "expiry")

        monkeypatch.setattr(gateway_lifespan.asyncio, "create_task", create_task)
        value = app()
        with pytest.raises(asyncio.CancelledError):
            async with gateway_lifespan.create_lifespan(settings(), realtime())(value):
                raise asyncio.CancelledError()
        assert value.state.ready is False
        assert events == [
            "prepare",
            "hub.sync",
            "maintain.cancel",
            "expiry.cancel",
            "hub.shutdown",
            "redis.close",
            "database.close",
        ]

    asyncio.run(scenario())


def test_gateway_lifespan_cleans_first_task_when_second_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        events: list[str] = []
        install_fakes(monkeypatch, events)
        created = 0

        def create_task(coro):
            nonlocal created
            created += 1
            if created == 2:
                raise RuntimeError("expiry creation failed")
            coro.close()
            return Task(events, "maintain")

        monkeypatch.setattr(gateway_lifespan.asyncio, "create_task", create_task)
        value = app()
        with pytest.raises(RuntimeError, match="expiry creation failed"):
            async with gateway_lifespan.create_lifespan(settings(), realtime())(value):
                pytest.fail("startup must fail")
        assert value.state.ready is False
        assert events == [
            "prepare",
            "hub.sync",
            "maintain.cancel",
            "hub.shutdown",
            "redis.close",
            "database.close",
        ]

    asyncio.run(scenario())
