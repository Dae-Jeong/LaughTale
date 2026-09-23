"""내부 전달 토큰과 브라우저 Origin은 서로 다른 신뢰 경계입니다."""

import asyncio
import secrets
from http import HTTPStatus

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from chat_service.core.realtime_settings import RealtimeSettings, validate_ip
from chat_service.http.errors import problem_response
from chat_service.schemas.responses import ErrorCode


class GatewaySecurity:
    def __init__(self, app: ASGIApp, settings: RealtimeSettings) -> None:
        self.app, self.settings = app, settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        path = scope.get("path")
        if path in {"/health/live", "/health/ready", "/metrics"}:
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])

        def values(name: bytes) -> list[str]:
            return [
                value.decode("latin1") for key, value in headers if key.lower() == name
            ]

        allowed = not any(
            key.lower() == b"forwarded" or key.lower().startswith(b"x-forwarded-")
            for key, _ in headers
        )
        client = scope.get("client")
        try:
            if not client:
                raise ValueError("NO_PEER")
            validate_ip(
                client[0], True
            )  # kubectl port-forward의 loopback도 명시 허용합니다.
        except ValueError:
            allowed = False
        if path == "/internal/events" and scope["type"] == "http":
            auth = values(b"authorization")
            allowed = (
                allowed
                and scope.get("method") == "POST"
                and not values(b"origin")
                and len(auth) == 1
                and secrets.compare_digest(
                    auth[0],
                    "Bearer " + self.settings.gateway_delivery_token.get_secret_value(),
                )
            )
            # 내부 주소는 실제 Pod IP의 고정 포트이며 요청 헤더를 callback으로 사용하지 않습니다.
            allowed = allowed and values(b"host") == [
                f"{self.settings.gateway_ip}:18082"
            ]
        elif path == "/v1/ws" and scope["type"] == "websocket":
            allowed = (
                allowed
                and values(b"origin") == ["http://127.0.0.1:18083"]
                and values(b"host")
                == [f"127.0.0.1:{self.settings.gateway_browser_port}"]
            )
        else:
            allowed = False
        if not allowed:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await problem_response(
                    Request(scope),
                    status=HTTPStatus.FORBIDDEN,
                    code=ErrorCode.HTTP_ERROR,
                )(scope, receive, send)
            return
        if scope["type"] == "http":
            body = bytearray()
            try:
                async with asyncio.timeout(1):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        body.extend(message.get("body", b""))
                        if len(body) > 16384:
                            raise ValueError("BODY_LIMIT")
                        if not message.get("more_body", False):
                            break
            except ValueError, TimeoutError:
                await problem_response(
                    Request(scope),
                    status=HTTPStatus.CONTENT_TOO_LARGE,
                    code=ErrorCode.INVALID_INPUT,
                )(scope, receive, send)
                return
            delivered = False

            async def bounded_receive():
                nonlocal delivered
                if delivered:
                    return await receive()
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            await self.app(scope, bounded_receive, send)
        else:
            await self.app(scope, receive, send)
