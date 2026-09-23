import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from platform_mock.app import create_app
from platform_mock.settings import Settings


def settings(**changes: object) -> Settings:
    return Settings.model_validate(
        {
            "control_token": "control-synthetic-1234567890",
            "service_token": "service-synthetic-1234567890",
            **changes,
        }
    )


def test_local_profile_does_not_accept_cluster_callback() -> None:
    with pytest.raises(ValidationError):
        settings(chat_base_url="http://chat:18082")


@pytest.mark.parametrize("host", ["evil", "chat.evil", "169.254.169.254"])
def test_lab_callback_remains_allowlisted(host: str) -> None:
    with pytest.raises(ValidationError):
        settings(network_profile="isolated-lab", chat_base_url=f"http://{host}:18082")


def test_lab_auth_host_and_client_boundaries() -> None:
    app = create_app(
        settings(network_profile="isolated-lab", chat_base_url="http://chat:18082")
    )
    with TestClient(
        app, base_url="http://platform-mock:18087", client=("10.42.0.8", 12000)
    ) as client:
        assert client.get("/health/ready").status_code == 200
        assert (
            client.post("/control/v1/runs", json={"run_id": "test"}).status_code == 401
        )
        assert (
            client.get("/health/ready", headers={"Host": "evil:18087"}).status_code
            == 403
        )
        assert (
            client.get(
                "/health/ready", headers={"X-Forwarded-For": "127.0.0.1"}
            ).status_code
            == 403
        )
    with TestClient(
        app, base_url="http://platform-mock:18087", client=("192.168.0.1", 12000)
    ) as client:
        assert client.get("/health/ready").status_code == 403


def test_local_profile_still_rejects_pod_client() -> None:
    with TestClient(
        create_app(settings()),
        base_url="http://127.0.0.1:18087",
        client=("10.42.0.8", 12000),
    ) as client:
        assert client.get("/health/ready").status_code == 403
