from contextlib import AsyncExitStack
from uuid import UUID

import pytest
from fastapi import FastAPI
from platform_contracts.wire import ConnectionSeed
from pydantic import ValidationError
from starlette.testclient import TestClient

from chat_service.bootstrap.app import create_app
from chat_service.core.settings import Settings
from chat_service.dependencies.external import get_external_service

CONNECTION = UUID("00000000-0000-4000-8000-000000000001")
CONTROL = "synthetic-control-token"
TOKEN = "synthetic-connection-token"


def settings_values(**changes):
    return {
        "network_profile": "isolated-lab",
        "app_environment": "isolated-lab",
        "server_host": "0.0.0.0",
        "server_port": 18082,
        "dev_sessions_enabled": True,
        "external_enabled": True,
        "db_primary_url": "postgresql+asyncpg://synthetic:unused@127.0.0.1:5440/unused",
        "external_control_token": CONTROL,
        "mock_api_token": "synthetic-outbound-token",
        "mock_api_url": "http://platform-mock:18087",
        "external_connection_credentials": {
            str(CONNECTION): {"profile": "telegram", "token": TOKEN}
        },
        **changes,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"network_profile": "local"},
        {"app_environment": "production"},
        {"app_environment": "local"},
        {"server_host": "127.0.0.1"},
        {"server_port": 18083},
        {"mock_api_url": "http://evil.example:18087"},
        {"mock_api_url": "http://platform-mock.evil:18087"},
        {"mock_api_url": "http://platform-mock:18087/"},
        {"external_control_token": "has whitespace token"},
        {"external_control_token": "non-ascii-토큰token"},
        {"external_control_token": "control-with\nnewline"},
        {"external_control_token": "x" * 257},
    ],
)
def test_explicit_lab_settings_reject_unsafe_combinations(change):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **settings_values(**change))


def test_local_default_and_lab_dns_are_separate():
    assert Settings(_env_file=None).mock_api_url == "http://127.0.0.1:18087"
    assert Settings(_env_file=None).network_profile == "local"
    assert Settings(_env_file=None, **settings_values()).server_host == "0.0.0.0"
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            **settings_values(
                network_profile="local",
                app_environment="local",
                server_host="127.0.0.1",
            ),
        )


class Service:
    async def seed(self, body: ConnectionSeed) -> UUID:
        return body.connection_id


@pytest.mark.parametrize(
    "ip,host,extra,status",
    [
        ("10.42.0.2", "chat:18082", {}, 201),
        ("127.0.0.1", "127.0.0.1:18082", {}, 201),
        ("10.42.1.2", "chat:18082", {}, 403),
        ("192.0.2.1", "chat:18082", {}, 403),
        ("::1", "chat:18082", {}, 403),
        ("invalid", "chat:18082", {}, 403),
        ("10.42.0.2", "evil.example:18082", {}, 403),
        ("10.42.0.2", "chat:18083", {}, 403),
        ("10.42.0.2", "127.0.0.1:18082", {}, 403),
        ("10.42.0.2", "chat:18082", {"Origin": "http://127.0.0.1:18083"}, 403),
        ("10.42.0.2", "chat:18082", {"Forwarded": "for=127.0.0.1"}, 403),
        ("10.42.0.2", "chat:18082", {"X-Forwarded-For": "127.0.0.1"}, 403),
        ("10.42.0.2", "chat:18082", {"Authorization": ""}, 401),
        ("10.42.0.2", "chat:18082", {"Authorization": "Bearer wrong"}, 401),
    ],
)
def test_lab_machine_boundary(ip, host, extra, status):
    async def prepare(app: FastAPI, stack: AsyncExitStack):
        pass

    app = create_app(Settings(_env_file=None, **settings_values()), prepare=prepare)
    app.dependency_overrides[get_external_service] = Service
    with TestClient(app, base_url=f"http://{host}", client=(ip, 1234)) as client:
        result = client.post(
            "/v1/dev/external-connections",
            headers={"Authorization": "Bearer " + CONTROL, **extra},
            json={
                "run_id": "test",
                "profile": "telegram",
                "connection_id": str(CONNECTION),
                "external_conversation_id": "room",
                "external_sender_id": "customer",
                "operator_user_id": str(CONNECTION),
            },
        )
        assert result.status_code == status
        # 같은 Pod IP+Host라도 브라우저 세션/조회 API로 확장하지 않습니다.
        if ip != "127.0.0.1":
            assert client.get("/v1/session").status_code == 403
            assert client.get("/v1/external-conversations").status_code == 403


def test_lab_cookie_and_socket_remain_loopback_only():
    async def prepare(app: FastAPI, stack: AsyncExitStack):
        pass

    app = create_app(Settings(_env_file=None, **settings_values()), prepare=prepare)
    from starlette.websockets import WebSocketDisconnect

    with TestClient(
        app, base_url="http://chat:18082", client=("10.42.0.2", 1234)
    ) as client:
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect(
                "ws://chat:18082/v1/external-ws",
                headers={"Origin": "http://127.0.0.1:18083"},
            ):
                pass
        assert error.value.code == 1008
    with TestClient(
        app, base_url="http://127.0.0.1:18082", client=("127.0.0.1", 1234)
    ) as client:
        assert client.get("/v1/session").status_code == 401
