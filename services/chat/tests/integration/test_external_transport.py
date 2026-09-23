from contextlib import AsyncExitStack
from uuid import uuid4

import pytest
from fastapi import FastAPI
from platform_contracts.wire import DeliveryState
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from tests.integration.test_chat_service import service_database
from tests.integration.test_external_service import event_input, seed_input

from chat_service.adapters.mock_platform import MockPlatform
from chat_service.bootstrap.app import create_app
from chat_service.contracts.delivery import DeliveryResult
from chat_service.core.settings import Settings
from chat_service.services.external import ExternalService

pytestmark = pytest.mark.postgres
ORIGIN = {"Origin": "http://127.0.0.1:18083"}
CONTROL = "synthetic-control-token"
CONNECTION = "synthetic-connection-token"


def test_external_http_ws_and_machine_boundary(postgres_url: str, monkeypatch):
    seed = seed_input()

    async def prepare(app: FastAPI, stack: AsyncExitStack):
        database = await stack.enter_async_context(service_database(postgres_url))
        app.state.primary_session_factory = database.factory
        app.state.database_metrics = database.metrics

    async def accept(self, command):
        return DeliveryResult(DeliveryState.ACCEPTED, str(uuid4()))

    monkeypatch.setattr(MockPlatform, "send_message", accept)
    settings = Settings(
        _env_file=None,
        dev_sessions_enabled=True,
        db_primary_url=postgres_url,
        external_enabled=True,
        external_control_token=CONTROL,
        mock_api_token="synthetic-outbound-token",
        external_connection_credentials={
            str(seed.connection_id): {"profile": "telegram", "token": CONNECTION}
        },
    )
    app = create_app(settings, prepare=prepare)
    with TestClient(
        app, base_url="http://127.0.0.1:18082", client=("127.0.0.1", 1234)
    ) as client:
        assert (
            client.post(
                "/v1/dev/session", json={"user": "user_a"}, headers=ORIGIN
            ).status_code
            == 200
        )
        seed_body = seed.model_dump(mode="json")
        assert (
            client.post("/v1/dev/external-connections", json=seed_body).status_code
            == 401
        )
        assert (
            client.post(
                "/v1/dev/external-connections",
                json=seed_body,
                headers={"Authorization": "Bearer " + CONNECTION},
            ).status_code
            == 401
        )
        response = client.post(
            "/v1/dev/external-connections",
            json=seed_body,
            headers={"Authorization": "Bearer " + CONTROL},
        )
        assert response.status_code == 201
        room = response.json()["data"]["conversation_id"]
        assert (
            client.get("/v1/external-conversations").json()["data"][0][
                "conversation_id"
            ]
            == room
        )
        event = event_input(seed).model_dump(mode="json")
        bearer = {"Authorization": "Bearer " + CONNECTION}
        assert (
            client.post(
                "/v1/external-events", json=event, headers=ORIGIN | bearer
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/v1/external-events",
                json=event,
                headers={"Authorization": "Bearer " + CONTROL},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/v1/external-events",
                json=event,
                headers=bearer | {"X-Forwarded-For": "127.0.0.1"},
            ).status_code
            == 403
        )
        with client.websocket_connect(
            "ws://127.0.0.1:18082/v1/external-ws", headers=ORIGIN
        ) as socket:
            socket.send_json({"type": "subscribe", "conversation_id": room})
            assert socket.receive_json() == {
                "type": "head",
                "conversation_id": room,
                "head_seq": "0",
            }
            response = client.post("/v1/external-events", json=event, headers=bearer)
            assert response.status_code == 201
            assert response.json()["data"]["external_sender_id"] == "customer"
            assert (
                client.post(
                    "/v1/external-events", json=event, headers=bearer
                ).status_code
                == 200
            )
            assert socket.receive_json()["head_seq"] == "1"
            path = f"/v1/external-conversations/{room}/messages"
            reply = client.post(
                path,
                json={"client_message_id": str(uuid4()), "text": "reply"},
                headers=ORIGIN,
            )
            assert reply.status_code == 201
            assert reply.json()["data"]["operation_id"] is not None
            assert socket.receive_json()["head_seq"] == "2"
            message_id = reply.json()["data"]["message_id"]
            status = client.get(path + "/" + message_id).json()["data"]
            assert status["delivery_state"] == "accepted" and status["effect_id"]
            assert [item["seq"] for item in client.get(path).json()["data"]] == [
                "1",
                "2",
            ]
            client.post("/v1/dev/session", json={"user": "user_b"}, headers=ORIGIN)
            assert client.get(path).status_code == 404
            assert client.get(path + "/" + message_id).status_code == 404
        assert app.state.external_slots.active == 0


def test_browser_cannot_access_another_runtime_connection(postgres_url: str):
    seed_a, seed_b = seed_input(), seed_input()

    async def prepare(app: FastAPI, stack: AsyncExitStack):
        database = await stack.enter_async_context(service_database(postgres_url))
        app.state.primary_session_factory = database.factory
        app.state.database_metrics = database.metrics
        setup = ExternalService(
            database,
            app.state.clock,
            connection_ids=frozenset({seed_a.connection_id, seed_b.connection_id}),
        )
        app.state.room_a = await setup.seed(seed_a)
        app.state.room_b = await setup.seed(seed_b)
        stored = await setup.receive(event_input(seed_b))
        app.state.message_b = stored.message.message_id
        # 같은 상담사가 두 방에 실제로 속하지만 runtime 설정은 A 연결만 소유합니다.
        assert len(await setup.rooms(seed_a.operator_user_id)) == 2

    settings = Settings(
        _env_file=None,
        dev_sessions_enabled=True,
        db_primary_url=postgres_url,
        external_enabled=True,
        external_control_token=CONTROL,
        mock_api_token="synthetic-outbound-token",
        external_connection_credentials={
            str(seed_a.connection_id): {"profile": "telegram", "token": CONNECTION}
        },
    )
    app = create_app(settings, prepare=prepare)
    with TestClient(
        app, base_url="http://127.0.0.1:18082", client=("127.0.0.1", 1234)
    ) as client:
        assert (
            client.post(
                "/v1/dev/session", json={"user": "user_a"}, headers=ORIGIN
            ).status_code
            == 200
        )
        rooms = client.get("/v1/external-conversations").json()["data"]
        assert [room["conversation_id"] for room in rooms] == [str(app.state.room_a)]
        good = f"/v1/external-conversations/{app.state.room_a}/messages"
        other = f"/v1/external-conversations/{app.state.room_b}/messages"
        assert client.get(good).status_code == 200
        assert client.get(other).status_code == 404
        assert client.get(other + f"/{app.state.message_b}").status_code == 404
        assert client.get(good + f"/{app.state.message_b}").status_code == 404
        assert (
            client.post(
                other,
                json={"client_message_id": str(uuid4()), "text": "must not enqueue"},
                headers=ORIGIN,
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/v1/external-events",
                json=event_input(seed_b).model_dump(mode="json"),
                headers={"Authorization": "Bearer " + CONNECTION},
            ).status_code
            == 403
        )
        with client.websocket_connect(
            "ws://127.0.0.1:18082/v1/external-ws", headers=ORIGIN
        ) as socket:
            socket.send_json(
                {"type": "subscribe", "conversation_id": str(app.state.room_b)}
            )
            with pytest.raises(WebSocketDisconnect) as error:
                socket.receive_json()
            assert error.value.code == 1008
        assert app.state.external_slots.active == 0
