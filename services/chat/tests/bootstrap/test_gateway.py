import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import httpx2
import pytest
from fastapi import FastAPI

from chat_service.bootstrap.gateway import create_gateway
from chat_service.core.realtime_settings import RealtimeSettings
from chat_service.core.settings import Settings
from chat_service.schemas.chat import MessageCreated, MessageData
from chat_service.schemas.responses import Problem


def assert_problem(response: httpx2.Response, status: int, code: str) -> None:
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    problem = Problem.model_validate(response.json())
    assert problem.status == status and problem.code == code
    assert problem.type == "about:blank" and problem.title
    assert problem.request_id == response.headers["x-request-id"]
    assert len(problem.request_id) == 32
    assert set(response.json()) <= {
        "type",
        "title",
        "status",
        "code",
        "request_id",
        "errors",
    }


@pytest.mark.parametrize(
    "failure", ["authorization", "body_size", "timeout", "unexpected"]
)
def test_all_http_failure_boundaries_use_problem(failure):
    class FailingHub(Hub):
        async def deliver(self, event):
            raise RuntimeError("synthetic-private-error")

    async def scenario():
        app = gateway(FailingHub())
        token = realtime().gateway_delivery_token.get_secret_value()
        headers = {
            "Authorization": "Bearer " + token,
            "x-gateway-instance-id": str(app.state.target.instance_id),
        }
        if failure == "authorization":
            headers.pop("Authorization")

        async def slow_body():
            await asyncio.sleep(2)
            yield b"{}"

        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(
                app=app, client=("127.0.0.1", 1234), raise_app_exceptions=False
            ),
            base_url="http://127.0.0.1:18082",
        ) as client:
            if failure == "body_size":
                response = await client.post(
                    "/internal/events", headers=headers, content=b"x" * 16385
                )
            elif failure == "timeout":
                response = await client.post(
                    "/internal/events", headers=headers, content=slow_body()
                )
            else:
                response = await client.post(
                    "/internal/events",
                    headers=headers,
                    json={
                        "instance_id": str(app.state.target.instance_id),
                        "event": event().model_dump(mode="json"),
                    },
                )
        status, code = {
            "authorization": (403, "HTTP_ERROR"),
            "body_size": (413, "INVALID_INPUT"),
            "timeout": (413, "INVALID_INPUT"),
            "unexpected": (500, "INTERNAL_ERROR"),
        }[failure]
        assert_problem(response, status, code)
        assert (
            "synthetic-private-error" not in response.text
            and token not in response.text
        )

    asyncio.run(scenario())


class Hub:
    def __init__(self, *, healthy: bool = True, outcome: str = "accepted") -> None:
        self.is_healthy = healthy
        self.outcome = outcome

    def healthy(self) -> bool:
        return self.is_healthy

    async def deliver(self, event: MessageCreated) -> str:
        return self.outcome


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


def gateway(hub: Hub) -> FastAPI:
    app = create_gateway(
        Settings(
            _env_file=None, db_primary_url="postgresql+asyncpg://test:test@db/test"
        ),
        realtime(),
    )
    app.state.chat_hub = hub
    app.state.ready = True
    return app


def event() -> MessageCreated:
    identity = uuid4()
    return MessageCreated(
        event_id=identity,
        message=MessageData(
            message_id=identity,
            conversation_id=uuid4(),
            sender_id=uuid4(),
            client_message_id=uuid4(),
            seq="1",
            text="synthetic",
            created_at=datetime.now(UTC),
        ),
    )


def test_gateway_openapi_describes_common_problem():
    schema = gateway(Hub()).openapi()
    responses = schema["paths"]["/internal/events"]["post"]["responses"]
    for status in (403, 409, 413, 422, 500, 503):
        content = responses[str(status)]["content"]
        assert (
            content["application/problem+json"]["schema"]["$ref"]
            == "#/components/schemas/Problem"
        )
        assert "application/json" not in content


def test_create_gateway_preserves_health_and_internal_delivery_contract():
    async def scenario() -> None:
        hub = Hub()
        app = gateway(hub)
        instance = str(app.state.target.instance_id)
        headers = {
            "Authorization": "Bearer "
            + realtime().gateway_delivery_token.get_secret_value(),
            "x-gateway-instance-id": instance,
        }
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://127.0.0.1:18082",
        ) as client:
            assert (await client.get("/health/live")).json() == {
                "status": "alive",
                "instance_id": instance,
            }
            assert (await client.get("/health/ready")).json() == {
                "status": "ready",
                "instance_id": instance,
            }
            message = event()
            delivered = await client.post(
                "/internal/events",
                headers=headers,
                json={
                    "instance_id": instance,
                    "event": message.model_dump(mode="json"),
                },
            )
            assert delivered.status_code == 200
            assert delivered.json()["data"] == {
                "instance_id": instance,
                "event_id": str(message.event_id),
                "outcome": "accepted",
            }
            wrong_instance = await client.post(
                "/internal/events",
                headers=headers,
                json={
                    "instance_id": str(uuid4()),
                    "event": event().model_dump(mode="json"),
                },
            )
            assert wrong_instance.status_code == 409
            assert_problem(wrong_instance, 409, "INSTANCE_MISMATCH")
            mismatched = event()
            mismatched = mismatched.model_copy(update={"event_id": uuid4()})
            bad_identity = await client.post(
                "/internal/events",
                headers=headers,
                json={
                    "instance_id": instance,
                    "event": mismatched.model_dump(mode="json"),
                },
            )
            assert bad_identity.status_code == 422
            assert_problem(bad_identity, 422, "EVENT_IDENTITY_MISMATCH")
            hub.is_healthy = False
            unavailable = await client.post(
                "/internal/events",
                headers=headers,
                json={
                    "instance_id": instance,
                    "event": event().model_dump(mode="json"),
                },
            )
            assert unavailable.status_code == 503
            assert_problem(unavailable, 503, "REGISTRY_UNAVAILABLE")

        # Inspect the assembled routing table through FastAPI's schema traversal;
        # router includes can be represented lazily in app.routes.
        assert set(app.openapi()["paths"]) == {
            "/health/live",
            "/health/ready",
            "/internal/events",
            "/metrics",
        }

    asyncio.run(scenario())


@pytest.mark.parametrize("headers_kind", ["missing", "wrong", "duplicate"])
def test_instance_header_must_be_one_matching_value(headers_kind: str) -> None:
    async def scenario() -> None:
        app = gateway(Hub())
        instance = str(app.state.target.instance_id)
        headers = [
            (
                "Authorization",
                "Bearer " + realtime().gateway_delivery_token.get_secret_value(),
            )
        ]
        if headers_kind != "missing":
            headers.append(
                (
                    "x-gateway-instance-id",
                    str(uuid4()) if headers_kind == "wrong" else instance,
                )
            )
        if headers_kind == "duplicate":
            headers.append(("x-gateway-instance-id", instance))
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://127.0.0.1:18082",
        ) as client:
            response = await client.post(
                "/internal/events",
                headers=headers,
                json={
                    "instance_id": instance,
                    "event": event().model_dump(mode="json"),
                },
            )
            assert response.status_code == 409
            assert_problem(response, 409, "INSTANCE_MISMATCH")

    asyncio.run(scenario())


def test_gateway_timeout_readiness_and_security_boundaries() -> None:
    class TimeoutHub(Hub):
        async def deliver(self, event: MessageCreated) -> str:
            raise TimeoutError()

    async def scenario() -> None:
        hub = TimeoutHub()
        app = gateway(hub)
        instance = str(app.state.target.instance_id)
        headers = {
            "Authorization": "Bearer "
            + realtime().gateway_delivery_token.get_secret_value(),
            "x-gateway-instance-id": instance,
        }
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://127.0.0.1:18082",
        ) as client:
            response = await client.post(
                "/internal/events",
                headers=headers,
                json={
                    "instance_id": instance,
                    "event": event().model_dump(mode="json"),
                },
            )
            assert response.status_code == 503
            assert_problem(response, 503, "RESYNC_NOT_CONFIRMED")
            for ready, healthy in [(False, True), (True, False)]:
                app.state.ready, hub.is_healthy = ready, healthy
                response = await client.get("/health/ready")
                assert response.status_code == 503
                assert response.json() == {
                    "status": "unavailable",
                    "instance_id": instance,
                }
            assert (await client.post("/internal/events", json={})).status_code == 403
            # These assertions cover middleware rejection; route absence is
            # independently checked by the OpenAPI path set above.
            for path in ["/v1/conversations", "/v1/dev/session", "/v1/external-ws"]:
                assert (await client.post(path, json={})).status_code == 403

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["body", "header", "target", "identity", "health"])
def test_multiple_failures_preserve_validation_order(failure):
    class UnavailableHub(Hub):
        async def deliver(self, event):
            pytest.fail("Invalid request must not reach delivery")

    async def scenario():
        app = gateway(UnavailableHub(healthy=False))
        instance = str(app.state.target.instance_id)
        bad_event = event().model_copy(update={"event_id": uuid4()})
        body = {"instance_id": instance, "event": bad_event.model_dump(mode="json")}
        header = instance
        if failure == "body":
            body = {}
            header = str(uuid4())
        elif failure == "header":
            header = str(uuid4())
        elif failure == "target":
            header = str(uuid4())
            body["instance_id"] = header
        elif failure == "health":
            body["event"] = event().model_dump(mode="json")
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://127.0.0.1:18082",
        ) as client:
            response = await client.post(
                "/internal/events",
                json=body,
                headers={
                    "Authorization": "Bearer "
                    + realtime().gateway_delivery_token.get_secret_value(),
                    "x-gateway-instance-id": header,
                },
            )
        if failure == "body":
            assert response.status_code == 422
            assert_problem(response, 422, "INVALID_INPUT")
        else:
            expected = {
                "header": (409, "INSTANCE_MISMATCH"),
                "target": (409, "INSTANCE_MISMATCH"),
                "identity": (422, "EVENT_IDENTITY_MISMATCH"),
                "health": (503, "REGISTRY_UNAVAILABLE"),
            }
            status, code = expected[failure]
            assert response.status_code == status
            assert_problem(response, status, code)

    asyncio.run(scenario())
