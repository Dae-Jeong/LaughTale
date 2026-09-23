import asyncio
import json
import socket
from contextlib import AsyncExitStack
from uuid import uuid4

import httpx2
import pytest
import uvicorn
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from tests.integration.test_chat_service import A, service_database
from websockets.asyncio.client import connect
from websockets.typing import Origin

from chat_service.bootstrap.app import create_app
from chat_service.core.sessions import SEED_ROOM
from chat_service.core.settings import Settings
from chat_service.http.observation import HttpObservation
from chat_service.services.chat import ChatService

pytestmark = pytest.mark.postgres
ORIGIN = {"Origin": "http://127.0.0.1:18083"}
ROOM_PATH = f"/v1/internal-conversations/{SEED_ROOM}/messages"


def transport_app(url: str, port: int = 18082) -> FastAPI:
    async def prepare(app: FastAPI, stack: AsyncExitStack) -> None:
        service = await stack.enter_async_context(service_database(url))
        app.state.primary_session_factory = service.factory
        app.state.database_metrics = service.metrics

    return create_app(
        Settings(dev_sessions_enabled=True, server_port=port), prepare=prepare
    )


def test_real_http_ws_replay_final_event_recovery_and_user_rotation(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = transport_app(postgres_url)
    with TestClient(
        HttpObservation(app, app.state.metrics, log_context=app.state.log_context),
        base_url="http://127.0.0.1:18082",
        client=("127.0.0.1", 1234),
    ) as client:
        assert client.get("/v1/session").status_code == 401
        session = client.post(
            "/v1/dev/session", json={"user": "user_a"}, headers=ORIGIN
        )
        assert session.status_code == 200 and session.json()["data"]["user_id"] == str(
            A
        )
        assert (
            "HttpOnly" in session.headers["set-cookie"]
            and "SameSite=strict" in session.headers["set-cookie"]
        )
        assert set(session.json()["data"]) == {"user_id", "display_name"}
        with client.websocket_connect(
            "ws://127.0.0.1:18082/v1/ws", headers=ORIGIN
        ) as socket_a:
            socket_a.send_json({"type": "subscribe", "conversation_id": str(SEED_ROOM)})
            assert socket_a.receive_json() == {
                "type": "subscribed",
                "conversation_id": str(SEED_ROOM),
                "head_seq": "0",
                "protocol_version": 1,
            }
            client.cookies.clear()
            assert (
                client.post(
                    "/v1/dev/session", json={"user": "user_b"}, headers=ORIGIN
                ).status_code
                == 200
            )
            with client.websocket_connect(
                "ws://127.0.0.1:18082/v1/ws", headers=ORIGIN
            ) as socket_b:
                socket_b.send_json(
                    {"type": "subscribe", "conversation_id": str(SEED_ROOM)}
                )
                assert socket_b.receive_json()["type"] == "subscribed"
                body = {"client_message_id": str(uuid4()), "text": "안녕😀\n"}
                ack = client.post(ROOM_PATH, json=body, headers=ORIGIN)
                assert ack.status_code == 201
                event_a, event_b = socket_a.receive_json(), socket_b.receive_json()
                expected = {
                    key: value
                    for key, value in ack.json()["data"].items()
                    if key != "state"
                }
                assert event_a["message"] == event_b["message"] == expected
                assert event_a["event_id"] == expected["message_id"]
                replay = client.post(ROOM_PATH, json=body, headers=ORIGIN)
                assert replay.status_code == 200 and replay.json() == ack.json()
                conflict = client.post(
                    ROOM_PATH, json={**body, "text": "different"}, headers=ORIGIN
                )
                assert (
                    conflict.status_code == 409
                    and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
                )
                # 마지막 이벤트를 실제로 누락시키고 다음 메시지 없이 권위 head로 발견합니다.
                with monkeypatch.context() as patch:
                    patch.setattr(app.state.chat_hub, "publish", lambda event: None)
                    dropped = client.post(
                        ROOM_PATH,
                        json={"client_message_id": str(uuid4()), "text": "lost tail"},
                        headers=ORIGIN,
                    )
                service = ChatService(
                    app.state.primary_session_factory, app.state.database_metrics
                )
                assert client.portal is not None
                client.portal.call(
                    app.state.chat_hub.reconcile, service, app.state.sessions
                )
                assert socket_a.receive_json()["items"][0]["head_seq"] == "2"
                assert socket_b.receive_json()["items"][0]["head_seq"] == "2"
                history = client.get(ROOM_PATH, params={"after_seq": "1"})
                assert (
                    history.json()["data"][0]["message_id"]
                    == dropped.json()["data"]["message_id"]
                )
                assert history.headers["cache-control"] == "no-store"
                assert history.headers["x-request-id"]
                socket_a.send_json(
                    {"type": "unsubscribe", "conversation_id": str(SEED_ROOM)}
                )
                socket_a.send_json({"type": "invalid"})
                assert socket_a.receive_json() == {
                    "type": "error",
                    "code": "INVALID_INPUT",
                }
                # B 세션 교체는 기존 B socket의 다음 delivery를 거절합니다.
                client.post("/v1/dev/session", json={"user": "user_a"}, headers=ORIGIN)
                client.portal.call(
                    app.state.chat_hub.reconcile, service, app.state.sessions
                )
                with pytest.raises(WebSocketDisconnect) as closed:
                    socket_b.receive_json()
                assert closed.value.code == 1008
        assert app.state.chat_hub.peers == set()


@pytest.mark.parametrize(
    "body",
    [
        {"client_message_id": str(uuid4()), "text": "x", "sender_id": str(A)},
        {"client_message_id": str(uuid4()), "text": "   "},
        {"client_message_id": str(uuid4()), "text": "\u0000"},
        {"client_message_id": "invalid", "text": "x"},
    ],
)
def test_invalid_http_input_has_no_side_effect(
    postgres_url: str, body: dict[str, str]
) -> None:
    app = transport_app(postgres_url)
    with TestClient(
        app, base_url="http://127.0.0.1:18082", client=("127.0.0.1", 1234)
    ) as client:
        client.post("/v1/dev/session", json={"user": "user_a"}, headers=ORIGIN)
        result = client.post(ROOM_PATH, json=body, headers=ORIGIN)
        assert result.status_code == 422 and result.json()["code"] == "INVALID_INPUT"
        assert client.get(ROOM_PATH).json()["meta"]["snapshot_head_seq"] == "0"


def test_publish_failure_preserves_ack_and_invalid_ws_auth(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = transport_app(postgres_url)
    with TestClient(
        app, base_url="http://127.0.0.1:18082", client=("127.0.0.1", 1234)
    ) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("ws://127.0.0.1:18082/v1/ws", headers=ORIGIN):
                pass
        client.post("/v1/dev/session", json={"user": "user_a"}, headers=ORIGIN)
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "ws://127.0.0.1:18082/v1/ws", headers={"Origin": "http://evil.example"}
            ):
                pass

        def fail(event) -> None:
            raise RuntimeError("synthetic publish failure")

        monkeypatch.setattr(app.state.chat_hub, "publish", fail)
        ack = client.post(
            ROOM_PATH,
            json={"client_message_id": str(uuid4()), "text": "stored"},
            headers=ORIGIN,
        )
        assert ack.status_code == 201
        assert len(client.get(ROOM_PATH).json()["data"]) == 1


def test_uvicorn_sansio_real_loopback_transport(postgres_url: str) -> None:
    """개발 서버를 건드리지 않고 임시 socket·격리 DB로 실제 upgrade를 검사합니다."""

    async def scenario() -> None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            app = transport_app(postgres_url, port)
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=port,
                    ws="websockets-sansio",
                    ws_max_size=16384,
                    proxy_headers=False,
                    access_log=False,
                    log_config=None,
                    log_level="critical",
                )
            )
            running = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(8):
                    while not server.started:
                        if running.done():
                            running.result()
                        await asyncio.sleep(0.01)
                    async with httpx2.AsyncClient(
                        base_url=f"http://127.0.0.1:{port}"
                    ) as client:
                        response = await client.post(
                            "/v1/dev/session", json={"user": "user_a"}, headers=ORIGIN
                        )
                        assert response.status_code == 200
                        token = response.cookies.get("chat_session")
                        async with connect(
                            f"ws://127.0.0.1:{port}/v1/ws",
                            origin=Origin(ORIGIN["Origin"]),
                            additional_headers={"Cookie": f"chat_session={token}"},
                        ) as websocket:
                            await websocket.send(
                                json.dumps(
                                    {
                                        "type": "subscribe",
                                        "conversation_id": str(SEED_ROOM),
                                    }
                                )
                            )
                            assert (
                                json.loads(await websocket.recv())["type"]
                                == "subscribed"
                            )
                            ack = await client.post(
                                ROOM_PATH,
                                json={
                                    "client_message_id": str(uuid4()),
                                    "text": "network",
                                },
                                headers=ORIGIN,
                            )
                            assert ack.status_code == 201
                            event = json.loads(await websocket.recv())
                            assert (
                                event["message"]["message_id"]
                                == ack.json()["data"]["message_id"]
                            )
            finally:
                server.should_exit = True
                async with asyncio.timeout(8):
                    await running

    asyncio.run(scenario())
