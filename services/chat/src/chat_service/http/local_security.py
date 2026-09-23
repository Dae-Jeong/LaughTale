import asyncio
import secrets
from http import HTTPStatus
from ipaddress import ip_address, ip_network
from uuid import UUID

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from chat_service.core.settings import Settings
from chat_service.http.errors import problem_response
from chat_service.schemas.responses import ErrorCode

BODY_TIMEOUT_SECONDS = 5
# 단일 node 전용 학습 cluster입니다. 다른 cluster의 CIDR로 자동 확장하지 않습니다.
LAB_POD_NETWORK = ip_network("10.42.0.0/24")


def ingress_route(path: str, method: str) -> bool:
    if path in {"/v1/session", "/v1/internal-conversations"}:
        return method == "GET"
    parts = path.split("/")
    if (
        len(parts) != 5
        or parts[:3] != ["", "v1", "internal-conversations"]
        or parts[4] != "messages"
        or method not in {"GET", "POST"}
    ):
        return False
    try:
        return str(UUID(parts[3])) == parts[3]
    except ValueError:
        return False


class LocalChatSecurity:
    """실험 API의 직접 loopback 연결·정확한 authority·Origin을 검사합니다."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        machine_path = path in {"/v1/external-events", "/v1/dev/external-connections"}
        external_path = (
            machine_path
            or path.startswith("/v1/external-conversations")
            or path == "/v1/external-ws"
        )
        chat_path = (
            path
            in {
                "/v1/session",
                "/v1/dev/session",
                "/v1/ws",
            }
            or path.startswith("/v1/internal-conversations")
            or external_path
        )
        if scope["type"] not in {"http", "websocket"} or not chat_path:
            await self.app(scope, receive, send)
            return
        headers = [(name.lower(), value) for name, value in scope.get("headers", [])]
        original_send = send
        ingress_peer = False

        async def no_store_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": [
                        (name, value)
                        for name, value in message.get("headers", [])
                        if name.lower() != b"cache-control"
                    ]
                    + [(b"cache-control", b"no-store")],
                }
                if ingress_peer:
                    message["headers"] = [
                        (name, value)
                        for name, value in message["headers"]
                        if name.lower() != b"x-lab-pod-uid"
                    ] + [
                        (
                            b"x-lab-pod-uid",
                            str(self.settings.lab_pod_uid).encode("ascii"),
                        )
                    ]
            await original_send(message)

        send = no_store_send
        hosts = [value.decode("latin1") for name, value in headers if name == b"host"]
        origins = [
            value.decode("latin1") for name, value in headers if name == b"origin"
        ]
        requires_origin = scope["type"] == "websocket" or scope.get("method") not in {
            "GET",
            "HEAD",
            "OPTIONS",
        }
        client = scope.get("client")
        local_peer = (
            client is not None
            and client[0] == "127.0.0.1"
            and hosts == [f"127.0.0.1:{self.settings.server_port}"]
        )
        lab_peer = False
        if (
            self.settings.network_profile == "isolated-lab"
            and machine_path
            and client is not None
            and hosts == ["chat:18082"]
        ):
            try:
                lab_peer = ip_address(client[0]) in LAB_POD_NETWORK
            except ValueError:
                pass
        ingress_tokens = [
            value for name, value in headers if name == b"x-lab-ingress-token"
        ]
        if (
            self.settings.lab_ingress_enabled
            and scope["type"] == "http"
            and ingress_route(path, scope.get("method", ""))
            and hosts == ["chat:18082"]
            and origins == [self.settings.dev_origin]
            and client is not None
            and len(ingress_tokens) == 1
            and secrets.compare_digest(
                ingress_tokens[0],
                self.settings.lab_ingress_token.get_secret_value().encode("ascii"),
            )
        ):
            try:
                ingress_peer = ip_address(client[0]) in LAB_POD_NETWORK
            except ValueError:
                pass
        allowed = (
            self.settings.dev_sessions_enabled
            and (not external_path or self.settings.external_enabled)
            and (local_peer or lab_peer or ingress_peer)
            and not any(
                name == b"forwarded" or name.startswith(b"x-forwarded-")
                for name, _ in headers
            )
            and (
                not origins
                if machine_path
                else (
                    origins == [self.settings.dev_origin]
                    if requires_origin or origins
                    else True
                )
            )
        )
        if not allowed:
            ingress_peer = False
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                response = problem_response(
                    Request(scope),
                    status=HTTPStatus.FORBIDDEN
                    if self.settings.dev_sessions_enabled
                    else HTTPStatus.NOT_FOUND,
                    code=ErrorCode.HTTP_ERROR,
                )
                await response(scope, receive, send)
            return
        if ingress_peer:
            # Credential은 업무 라우트와 그 이후 관측/응답에 전달하지 않습니다.
            scope = {
                **scope,
                "headers": [
                    (name, value)
                    for name, value in headers
                    if name != b"x-lab-ingress-token"
                ],
            }
        if scope["type"] == "http" and scope.get("method") == "POST":
            body = bytearray()
            try:
                async with asyncio.timeout(BODY_TIMEOUT_SECONDS):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        body.extend(message.get("body", b""))
                        if len(body) > 16384:
                            await problem_response(
                                Request(scope),
                                status=HTTPStatus.CONTENT_TOO_LARGE,
                                code=ErrorCode.HTTP_ERROR,
                            )(scope, receive, send)
                            return
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                await problem_response(
                    Request(scope),
                    status=HTTPStatus.REQUEST_TIMEOUT,
                    code=ErrorCode.HTTP_ERROR,
                )(scope, receive, send)
                return

            delivered = False

            async def bounded_receive() -> Message:
                nonlocal delivered
                if delivered:
                    return await receive()
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            await self.app(scope, bounded_receive, send)
        else:
            await self.app(scope, receive, send)
