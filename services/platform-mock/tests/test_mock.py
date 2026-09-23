import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from platform_contracts.wire import InboundEvent, OutboundCommand, Profile, RunConfig
from pydantic import SecretStr, ValidationError

from platform_mock.app import create_app
from platform_mock.ledger import Run
from platform_mock.settings import Settings

CONTROL = "control-test-token-1234567890"
SERVICE = "service-test-token-1234567890"
CONNECTION = uuid4()


def settings(**changes: object) -> Settings:
    return Settings.model_validate(
        {
            "control_token": CONTROL,
            "service_token": SERVICE,
            "connection_tokens": {
                CONNECTION: SecretStr("connection-test-token-1234567890")
            },
            **changes,
        }
    )


def command() -> dict[str, object]:
    return OutboundCommand(
        run_id="run1",
        profile=Profile.TELEGRAM,
        connection_id=CONNECTION,
        external_conversation_id="room",
        outbound_operation_id=uuid4(),
        text="synthetic reply",
    ).model_dump(mode="json")


def client(*, transport: httpx.AsyncBaseTransport | None = None) -> TestClient:
    return TestClient(
        create_app(settings(), transport=transport),
        base_url="http://127.0.0.1:18087",
        client=("127.0.0.1", 12345),
        headers={"Authorization": f"Bearer {CONTROL}"},
    )


def send(c: TestClient, body: dict[str, object]) -> httpx.Response:
    return c.post(
        "/mock/v1/messages", json=body, headers={"Authorization": f"Bearer {SERVICE}"}
    )


def test_idempotency_lookup_ledger() -> None:
    with client() as c:
        assert c.post("/control/v1/runs", json={"run_id": "run1"}).status_code == 201
        body = command()
        first, second = send(c, body), send(c, body)
        assert (first.status_code, second.status_code) == (201, 200)
        assert first.json() == second.json()
        assert send(c, {**body, "text": "different"}).status_code == 409
        looked = c.get(
            f"/mock/v1/operations/{body['outbound_operation_id']}",
            params={"run_id": "run1"},
            headers={"Authorization": f"Bearer {SERVICE}"},
        )
        assert looked.json() == first.json()
        page1 = c.get("/control/v1/runs/run1/ledger?limit=1").json()
        page2 = c.get("/control/v1/runs/run1/ledger?after=1").json()
        assert page1["meta"]["has_more"]
        assert len(page1["data"] + page2["data"]) == 4
        assert page2["meta"]["effects"] == 1


@pytest.mark.parametrize(
    ("fault", "status"), [("rate_limit", 429), ("unavailable", 503), ("reject", 403)]
)
def test_fault_first_attempt_only(fault: str, status: int) -> None:
    with client() as c:
        c.post("/control/v1/runs", json={"run_id": "run1", "fault": fault})
        body = command()
        assert send(c, body).status_code == status
        assert send(c, body).status_code == 201
        attempts = [
            row
            for row in c.get("/control/v1/runs/run1/ledger").json()["data"]
            if row["kind"] == "outbound_attempt"
        ]
        assert [row["applied_fault"] for row in attempts] == [fault, "none"]
        assert [row["injected_delay_seconds"] for row in attempts] == [0, 0]
        assert all(row["elapsed_seconds"] >= 0 for row in attempts)


def test_non_idempotent_and_unsupported_lookup() -> None:
    with client() as c:
        c.post(
            "/control/v1/runs",
            json={
                "run_id": "run1",
                "idempotency_supported": False,
                "lookup_supported": False,
            },
        )
        body = command()
        assert send(c, body).json() != send(c, body).json()
        assert (
            c.get(
                f"/mock/v1/operations/{body['outbound_operation_id']}?run_id=run1",
                headers={"Authorization": f"Bearer {SERVICE}"},
            ).status_code
            == 405
        )
        assert c.get("/control/v1/runs/run1/ledger").json()["meta"]["effects"] == 2


def test_security_and_bounds() -> None:
    with client() as c:
        assert c.get("/health/ready").status_code == 200
        assert c.post("/mock/v1/messages", json=command()).status_code == 401
        assert (
            c.get("/health/live", headers={"X-Forwarded-For": "127.0.0.1"}).status_code
            == 403
        )
        assert c.get("/health/live", headers={"Host": "evil.local"}).status_code == 403
        assert c.post("/control/v1/runs", content=b"x" * 16385).status_code == 413
        for index in range(4):
            assert (
                c.post("/control/v1/runs", json={"run_id": f"run{index}"}).status_code
                == 201
            )
        assert (
            c.post("/control/v1/runs", json={"run_id": "overflow"}).status_code == 429
        )
        assert c.delete("/control/v1/runs/run0").status_code == 200
        assert (
            c.post("/control/v1/runs", json={"run_id": "overflow"}).status_code == 201
        )


def test_effect_attempt_limits_no_eviction() -> None:
    with client() as c:
        c.post(
            "/control/v1/runs",
            json={"run_id": "run1", "max_effects": 1, "max_attempts": 3},
        )
        body = command()
        assert send(c, body).status_code == 201
        assert send(c, command()).status_code == 429
        assert send(c, body).status_code == 200
        assert send(c, body).status_code == 429
        assert c.get("/control/v1/runs/run1/ledger").json()["meta"]["effects"] == 1


def test_push_register_replay_and_connection_token() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"data": {"state": "stored"}})

    with client(transport=httpx.MockTransport(handler)) as c:
        c.post("/control/v1/runs", json={"run_id": "run1", "max_events": 1})
        event = InboundEvent(
            run_id="run1",
            profile=Profile.LINE,
            connection_id=CONNECTION,
            external_conversation_id="room",
            external_event_id="event1",
            external_message_id="msg1",
            external_sender_id="sender",
            occurred_at=datetime.now(UTC),
            text="synthetic inbound",
        ).model_dump(mode="json")
        route = "/control/v1/runs/run1/events"
        assert c.post(route, json=event).status_code == 201
        assert c.post(route, json=event).status_code == 200
        assert c.post(route, json={**event, "text": "different"}).status_code == 409
        assert (
            c.post(route, json={**event, "external_event_id": "event2"}).status_code
            == 429
        )
        for _ in range(2):
            assert (
                c.post(f"{route}/event1/deliver").json()["data"]["result"]
                == "acknowledged"
            )
        assert len(seen) == 2
        assert seen[0].url == "http://127.0.0.1:18082/v1/external-events"
        assert (
            seen[0].headers["authorization"]
            == "Bearer connection-test-token-1234567890"
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("chat_base_url", "https://example.com"),
        ("chat_base_url", "http://127.0.0.1:18082/redirect"),
        ("service_token", CONTROL),
    ],
)
def test_bad_settings(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        settings(**{field: value})


def test_delay_after_effect_cancel_preserves_effect() -> None:
    async def execute() -> None:
        entered = asyncio.Event()

        async def wait(_: float) -> None:
            entered.set()
            await asyncio.Event().wait()

        app = create_app(settings(), sleep=wait)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:18087"
            ) as c:
                await c.post(
                    "/control/v1/runs",
                    json={"run_id": "run1", "fault": "delay_after"},
                    headers={"Authorization": f"Bearer {CONTROL}"},
                )
                task = asyncio.create_task(
                    c.post(
                        "/mock/v1/messages",
                        json=command(),
                        headers={"Authorization": f"Bearer {SERVICE}"},
                    )
                )
                await entered.wait()
                assert app.state.ledger.get("run1").effect_count == 1
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert app.state.ledger.active == 0
                assert (
                    app.state.ledger.get("run1").records[-1]["result"]
                    == "effect_created"
                )
                evidence = app.state.ledger.get("run1").records[-1]
                assert evidence["applied_fault"] == "delay_after"
                assert evidence["injected_delay_seconds"] == 3
                assert evidence["elapsed_seconds"] >= 0

    asyncio.run(execute())


@pytest.mark.parametrize("fault", ["delay_before", "delay_after"])
def test_delay_fault_evidence_is_per_attempt(fault: str, monkeypatch) -> None:
    ticks = [10.0]
    requested = []

    async def sleep(seconds: float) -> None:
        requested.append(seconds)
        ticks[0] += seconds

    monkeypatch.setattr("platform_mock.app.monotonic", lambda: ticks[0])
    app = create_app(settings(), sleep=sleep)
    with TestClient(
        app,
        base_url="http://127.0.0.1:18087",
        client=("127.0.0.1", 12345),
        headers={"Authorization": f"Bearer {CONTROL}"},
    ) as c:
        c.post(
            "/control/v1/runs",
            json={
                "run_id": "run1",
                "fault": fault,
                "fault_attempts": 1,
                "delay_seconds": 3,
            },
        )
        body = command()
        assert send(c, body).status_code == 201
        assert send(c, body).status_code == 200
        records = c.get("/control/v1/runs/run1/ledger").json()["data"]
        attempts = [row for row in records if row["kind"] == "outbound_attempt"]
        assert requested == [3]
        assert [row["applied_fault"] for row in attempts] == [fault, "none"]
        assert [row["injected_delay_seconds"] for row in attempts] == [3, 0]
        assert [row["elapsed_seconds"] for row in attempts] == [3, 0]
        assert CONTROL not in str(records) and SERVICE not in str(records)


def test_deterministic_effect_id() -> None:
    cmd = OutboundCommand.model_validate(command())
    a, b = (
        Run(RunConfig(run_id="run1", seed=42)),
        Run(RunConfig(run_id="run1", seed=42)),
    )
    assert a.accept(cmd) == b.accept(cmd)


def test_concurrent_replay_and_capacity_cleanup() -> None:
    async def execute() -> None:
        app = create_app(settings())
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12)),
                base_url="http://127.0.0.1:18087",
            ) as c:
                await c.post(
                    "/control/v1/runs",
                    json={"run_id": "run1"},
                    headers={"Authorization": f"Bearer {CONTROL}"},
                )
                body = command()
                results = await asyncio.gather(
                    *(
                        c.post(
                            "/mock/v1/messages",
                            json=body,
                            headers={"Authorization": f"Bearer {SERVICE}"},
                        )
                        for _ in range(20)
                    )
                )
                assert sum(response.status_code == 201 for response in results) == 1
                assert sum(response.status_code == 200 for response in results) == 19
                assert app.state.ledger.get("run1").effect_count == 1
                assert app.state.ledger.active == 0
        assert not app.state.ready
        assert app.state.client.is_closed

    asyncio.run(execute())


def test_push_timeout_is_unknown_no_hidden_retry() -> None:
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise httpx.ReadTimeout("synthetic timeout")

    with client(transport=httpx.MockTransport(handler)) as c:
        c.post("/control/v1/runs", json={"run_id": "run1"})
        event = InboundEvent(
            run_id="run1",
            profile=Profile.LINE,
            connection_id=CONNECTION,
            external_conversation_id="room",
            external_event_id="event1",
            external_message_id="msg1",
            external_sender_id="sender",
            occurred_at=datetime.now(UTC),
            text="synthetic inbound",
        ).model_dump(mode="json")
        c.post("/control/v1/runs/run1/events", json=event)
        response = c.post("/control/v1/runs/run1/events/event1/deliver")
        assert response.json()["data"]["result"] == "unknown"
        assert len(seen) == 1
        assert c.get("/control/v1/runs/run1/ledger").json()["meta"]["active"] == 0
