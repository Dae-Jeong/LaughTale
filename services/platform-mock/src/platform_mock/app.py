import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from http import HTTPStatus
from time import monotonic
from uuid import UUID

import httpx2 as httpx
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from platform_contracts.wire import Fault, InboundEvent, OutboundCommand, RunConfig
from starlette.responses import JSONResponse

from platform_mock.contracts import (
    HealthStatus,
    InboundOutcome,
    MockError,
    OutboundOutcome,
)
from platform_mock.ledger import Ledger, Rejected
from platform_mock.security import LocalSecurity
from platform_mock.settings import Settings


def create_app(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> FastAPI:
    ledger = Ledger()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=settings.delivery_timeout_seconds,
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=16),
        ) as client:
            app.state.client = client
            app.state.ready = True
            try:
                yield
            finally:
                app.state.ready = False

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.ready = False
    app.state.ledger = ledger
    app.add_middleware(LocalSecurity, settings=settings)

    @app.exception_handler(Rejected)
    async def rejected(request: Request, exc: Rejected) -> JSONResponse:
        return JSONResponse(
            {"code": exc.code},
            status_code=exc.status,
            headers={"Retry-After": "1"}
            if exc.status == HTTPStatus.TOO_MANY_REQUESTS
            else None,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {"code": MockError.INVALID_INPUT},
            status_code=HTTPStatus.UNPROCESSABLE_CONTENT,
        )

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": HealthStatus.ALIVE}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        return JSONResponse(
            {
                "status": HealthStatus.READY
                if app.state.ready
                else HealthStatus.NOT_READY
            },
            status_code=HTTPStatus.OK
            if app.state.ready
            else HTTPStatus.SERVICE_UNAVAILABLE,
        )

    @app.post("/control/v1/runs")
    async def create(config: RunConfig) -> JSONResponse:
        ledger.create(config)
        return JSONResponse(
            {"data": config.model_dump(mode="json")}, status_code=HTTPStatus.CREATED
        )

    @app.delete("/control/v1/runs/{run_id}")
    async def delete(run_id: str) -> dict[str, object]:
        run = ledger.get(run_id)
        if run.active:
            raise Rejected(HTTPStatus.CONFLICT, MockError.RUN_ACTIVE)
        del ledger.runs[run_id]
        return {"data": {"deleted": True}}

    @app.post("/control/v1/runs/{run_id}/events")
    async def register(run_id: str, event: InboundEvent) -> JSONResponse:
        if event.run_id != run_id:
            raise Rejected(HTTPStatus.CONFLICT, MockError.RUN_MISMATCH)
        created = ledger.get(run_id).register(event)
        return JSONResponse(
            {"data": event.model_dump(mode="json")},
            status_code=HTTPStatus.CREATED if created else HTTPStatus.OK,
        )

    @app.post("/control/v1/runs/{run_id}/events/{external_event_id}/deliver")
    async def deliver(run_id: str, external_event_id: str) -> dict[str, object]:
        run = ledger.get(run_id)
        event = run.events.get(external_event_id)
        if event is None:
            raise Rejected(HTTPStatus.NOT_FOUND, MockError.EVENT_NOT_FOUND)
        token = settings.connection_tokens.get(event.connection_id)
        if token is None:
            raise Rejected(HTTPStatus.CONFLICT, MockError.CONNECTION_NOT_CONFIGURED)
        ledger.enter(run)
        started = False
        result = InboundOutcome.CANCELLED
        status: int | None = None
        attempt = 0
        try:
            attempt = run.start_attempt(f"in:{external_event_id}")
            started = True
            async with asyncio.timeout(settings.delivery_timeout_seconds):
                response = await app.state.client.post(
                    f"{settings.chat_base_url}/v1/external-events",
                    json=event.model_dump(mode="json"),
                    headers={"Authorization": f"Bearer {token.get_secret_value()}"},
                )
            status = response.status_code
            result = (
                InboundOutcome.ACKNOWLEDGED
                if status in {HTTPStatus.OK, HTTPStatus.CREATED}
                else InboundOutcome.REJECTED
                if 400 <= status < 500
                else InboundOutcome.UNKNOWN
            )
        except httpx.HTTPError, TimeoutError:
            result = InboundOutcome.UNKNOWN
        finally:
            if started:
                run.record(
                    "inbound_attempt",
                    external_event_id=external_event_id,
                    attempt=attempt,
                    result=result,
                    http_status=status,
                )
            ledger.leave(run)
        return {"data": {"result": result, "http_status": status, "attempt": attempt}}

    @app.get("/control/v1/runs/{run_id}/ledger")
    async def read_ledger(
        run_id: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> dict[str, object]:
        run = ledger.get(run_id)
        if after > len(run.records):
            raise Rejected(HTTPStatus.UNPROCESSABLE_CONTENT, MockError.INVALID_CURSOR)
        records = run.records[after : after + limit]
        cursor = after + len(records)
        return {
            "data": records,
            "meta": {
                "next_cursor": cursor,
                "has_more": cursor < len(run.records),
                "events": len(run.events),
                "attempts": run.attempt_count,
                "effects": run.effect_count,
                "active": run.active,
            },
        }

    @app.post("/mock/v1/messages")
    async def outbound(command: OutboundCommand) -> JSONResponse:
        run = ledger.get(command.run_id)
        ledger.enter(run)
        started = False
        outcome: OutboundOutcome | MockError = OutboundOutcome.CANCELLED
        attempt = 0
        applied_fault = Fault.NONE
        injected_delay_seconds = 0.0
        started_at = monotonic()
        try:
            attempt = run.start_attempt(f"out:{command.outbound_operation_id}")
            started = True
            fault = (
                run.config.fault if attempt <= run.config.fault_attempts else Fault.NONE
            )
            if fault in {Fault.RATE_LIMIT, Fault.UNAVAILABLE, Fault.REJECT}:
                applied_fault = fault
                raise Rejected(
                    {
                        Fault.RATE_LIMIT: HTTPStatus.TOO_MANY_REQUESTS,
                        Fault.UNAVAILABLE: HTTPStatus.SERVICE_UNAVAILABLE,
                        Fault.REJECT: HTTPStatus.FORBIDDEN,
                    }[fault],
                    MockError.INJECTED_FAILURE,
                )
            if fault == Fault.DELAY_BEFORE:
                applied_fault = fault
                injected_delay_seconds = run.config.delay_seconds
                await sleep(run.config.delay_seconds)
            effect, created = run.accept(command)
            outcome = (
                OutboundOutcome.EFFECT_CREATED if created else OutboundOutcome.REPLAYED
            )
            if fault == Fault.DELAY_AFTER:
                applied_fault = fault
                injected_delay_seconds = run.config.delay_seconds
                await sleep(run.config.delay_seconds)
            return JSONResponse(
                {"data": effect.model_dump(mode="json")},
                status_code=HTTPStatus.CREATED if created else HTTPStatus.OK,
            )
        except Rejected as exc:
            outcome = exc.code
            raise
        finally:
            if started:
                run.record(
                    "outbound_attempt",
                    outbound_operation_id=str(command.outbound_operation_id),
                    attempt=attempt,
                    result=outcome,
                    applied_fault=applied_fault.value,
                    injected_delay_seconds=injected_delay_seconds,
                    elapsed_seconds=monotonic() - started_at,
                )
            ledger.leave(run)

    @app.get("/mock/v1/operations/{operation_id}")
    async def lookup(operation_id: UUID, run_id: str) -> dict[str, object]:
        run = ledger.get(run_id)
        if not run.config.lookup_supported:
            raise Rejected(HTTPStatus.METHOD_NOT_ALLOWED, MockError.LOOKUP_UNSUPPORTED)
        existing = run.operations.get(operation_id)
        if existing is None:
            raise Rejected(HTTPStatus.NOT_FOUND, MockError.OPERATION_NOT_FOUND)
        return {"data": existing[1].model_dump(mode="json")}

    return app
