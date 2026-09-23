import asyncio
from contextlib import AsyncExitStack
from typing import cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from chat_service.bootstrap import lifespan
from chat_service.bootstrap.app import create_app
from chat_service.contracts.chat import Actor
from chat_service.core.database_metrics import create_database_metrics
from chat_service.core.settings import Settings
from chat_service.dependencies import connections
from chat_service.dependencies.chat import ChatServiceDep, create_service
from chat_service.dependencies.connections import (
    get_chat_connection,
    get_external_connection,
)
from chat_service.exceptions.chat import UnauthenticatedError
from chat_service.exceptions.database import ChatResourcesUnavailable
from chat_service.schemas.chat import Subscribe


def resource_app() -> FastAPI:
    app = create_app(Settings())
    app.state.database_metrics = create_database_metrics(app.state.metrics, 4)
    return app


@pytest.mark.parametrize("missing", ["primary_session_factory", "database_metrics"])
@pytest.mark.parametrize("value", ["absent", None])
def test_unavailable_resources_are_internal_or_http_errors(missing, value) -> None:
    app = resource_app()
    app.state.primary_session_factory = async_sessionmaker()
    if value == "absent":
        delattr(app.state, missing)
    else:
        setattr(app.state, missing, value)
    with pytest.raises(ChatResourcesUnavailable):
        create_service(app)

    @app.get("/dependency-check")
    def check(service: ChatServiceDep) -> dict[str, bool]:
        return {"created": True}

    assert TestClient(app).get("/dependency-check").status_code == 503


def test_service_creation_is_per_call_app_scoped_and_does_not_open_sessions() -> None:
    first, second = resource_app(), resource_app()
    factories = [Mock(spec=async_sessionmaker), Mock(spec=async_sessionmaker)]
    first.state.primary_session_factory, second.state.primary_session_factory = (
        factories
    )
    services = [create_service(first), create_service(first), create_service(second)]
    assert services[0] is not services[1]
    assert services[0].factory is services[1].factory is factories[0]
    assert services[2].factory is factories[1]
    assert services[0].metrics is first.state.database_metrics
    assert services[2].metrics is second.state.database_metrics
    assert first.state.ready is second.state.ready is False
    for factory in factories:
        factory.assert_not_called()


class Socket:
    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.cookies: dict[str, str] = {}
        self.closed: int | None = None

    async def close(self, *, code: int) -> None:
        self.closed = code

    async def accept(self) -> None:
        raise AssertionError("Unavailable or unauthenticated socket must not accept")


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("authenticated", [False, True])
@pytest.mark.parametrize("missing", ["primary_session_factory", "database_metrics"])
def test_socket_auth_precedes_resource_check_and_preserves_counters(
    external: bool, authenticated: bool, missing: str
) -> None:
    async def scenario() -> None:
        app = resource_app()
        app.state.primary_session_factory = async_sessionmaker()
        delattr(app.state, missing)
        calls: list[str] = []

        class Sessions:
            async def resolve(self, token: str | None) -> Actor:
                calls.append("resolve")
                if not authenticated:
                    raise UnauthenticatedError()
                return Actor(uuid4(), "synthetic")

        app.state.sessions = Sessions()
        app.state.external_slots.active = 7
        socket = Socket(app)
        get_connection = get_external_connection if external else get_chat_connection
        await get_connection(cast(WebSocket, socket)).run(cast(WebSocket, socket))
        assert calls == ["resolve"]
        assert socket.closed == (1013 if authenticated else 1008)
        assert app.state.external_slots.active == 7

    asyncio.run(scenario())


def test_external_socket_limit_precedes_auth_and_does_not_increment() -> None:
    async def scenario() -> None:
        app = create_app(Settings())
        app.state.external_slots.active = 128
        app.state.sessions = Mock()
        socket = Socket(app)
        await get_external_connection(cast(WebSocket, socket)).run(
            cast(WebSocket, socket)
        )
        assert socket.closed == 1013
        assert app.state.external_slots.active == 128
        app.state.sessions.resolve.assert_not_called()

    asyncio.run(scenario())


def test_external_socket_uses_common_service_and_releases_connection(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        app = resource_app()
        factory = Mock(spec=async_sessionmaker)
        app.state.primary_session_factory = factory
        actor, room = Actor(uuid4(), "synthetic"), uuid4()
        created = []

        class Sessions:
            async def resolve(self, token):
                return actor

        class External:
            def __init__(self, chat, clock, *, connection_ids):
                created.append(chat)

            async def head(self, user, conversation):
                assert user == actor.user_id and conversation == room
                return 0

        class ConnectedSocket(Socket):
            sent: list[str]

            async def accept(self):
                assert app.state.external_slots.active == 1
                self.sent = []

            async def receive_text(self):
                return Subscribe(conversation_id=room).model_dump_json()

            async def send_text(self, frame):
                self.sent.append(frame)

            async def receive(self):
                return {"type": "websocket.disconnect"}

        app.state.sessions = Sessions()
        monkeypatch.setattr(connections, "ExternalService", External)
        socket = ConnectedSocket(app)
        await get_external_connection(cast(WebSocket, socket)).run(
            cast(WebSocket, socket)
        )
        assert len(created) == len(socket.sent) == 1
        assert created[0].factory is factory
        assert created[0].metrics is app.state.database_metrics
        assert app.state.external_slots.active == 0
        assert socket.closed is None
        factory.assert_not_called()

    asyncio.run(scenario())


def test_optional_background_job_is_skipped_without_factory() -> None:
    app = create_app(Settings(dev_sessions_enabled=True))
    with TestClient(app):
        assert app.state.ready


def test_background_service_creation_does_not_depend_on_ready(monkeypatch) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        stopped = asyncio.Event()
        factory = Mock(spec=async_sessionmaker)

        async def prepare(app: FastAPI, stack: AsyncExitStack) -> None:
            app.state.primary_session_factory = factory
            app.state.database_metrics = create_database_metrics(app.state.metrics, 4)

        app = create_app(Settings(dev_sessions_enabled=True), prepare=prepare)

        def create_before_ready(value: FastAPI):
            assert value.state.ready is False
            return create_service(value)

        monkeypatch.setattr(lifespan, "create_service", create_before_ready)

        async def reconcile(service, sessions) -> None:
            assert service.factory is factory
            assert service.metrics is app.state.database_metrics
            entered.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        monkeypatch.setattr(app.state.chat_hub, "reconcile_loop", reconcile)
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(entered.wait(), 1)
            assert app.state.ready
            factory.assert_not_called()
        assert stopped.is_set()
        assert not app.state.ready

    asyncio.run(scenario())


def test_background_missing_metrics_fails_startup_and_cleans_resources() -> None:
    closed: list[bool] = []

    async def prepare(app: FastAPI, stack: AsyncExitStack) -> None:
        app.state.primary_session_factory = async_sessionmaker()
        app.state.database_metrics = None
        stack.callback(closed.append, True)

    app = create_app(Settings(dev_sessions_enabled=True), prepare=prepare)
    with pytest.raises(ChatResourcesUnavailable), TestClient(app):
        pytest.fail("Startup must not succeed with incomplete resources")
    assert closed == [True]
    assert not app.state.ready


@pytest.mark.parametrize("has_factory", [False, True])
def test_external_background_creation_and_optional_guard(
    monkeypatch, has_factory
) -> None:
    async def scenario() -> None:
        connection = uuid4()
        settings = Settings(
            _env_file=None,
            dev_sessions_enabled=True,
            db_primary_url="postgresql+asyncpg://test:test@db/test",
            external_enabled=True,
            external_control_token="synthetic-control-token",
            mock_api_token="synthetic-outbound-token",
            external_connection_credentials={
                str(connection): {
                    "profile": "telegram",
                    "token": "synthetic-connection-token",
                }
            },
        )
        factory = Mock(spec=async_sessionmaker)
        created = []
        entered, stopped = asyncio.Event(), asyncio.Event()

        async def prepare(app: FastAPI, stack: AsyncExitStack) -> None:
            if has_factory:
                app.state.primary_session_factory = factory

        app = create_app(settings, prepare=prepare)

        def assemble(value: FastAPI):
            assert not value.state.ready
            service = create_service(value)
            created.append(service)
            return service

        async def reconcile(service, sessions) -> None:
            await asyncio.Future()

        async def deliver(self) -> None:
            entered.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        monkeypatch.setattr(lifespan, "create_service", assemble)
        monkeypatch.setattr(app.state.chat_hub, "reconcile_loop", reconcile)
        monkeypatch.setattr(lifespan.ExternalDelivery, "run", deliver)
        async with app.router.lifespan_context(app):
            if has_factory:
                await asyncio.wait_for(entered.wait(), 1)
                assert len(created) == 2
                assert created[0] is not created[1]
                assert all(service.factory is factory for service in created)
            else:
                assert not created and not entered.is_set()
            factory.assert_not_called()
        assert stopped.is_set() is has_factory

    asyncio.run(scenario())
