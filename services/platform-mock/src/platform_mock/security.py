import asyncio
from http import HTTPStatus
from ipaddress import ip_address, ip_network
from secrets import compare_digest

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from platform_mock.contracts import MockError
from platform_mock.settings import Settings


class LocalSecurity:
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app, self.settings = app, settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope["headers"]

        async def reject(status: HTTPStatus) -> None:
            await JSONResponse(
                {"code": MockError.REQUEST_REJECTED}, status_code=status
            )(scope, receive, send)

        client = scope.get("client")
        allowed_hosts = [f"127.0.0.1:{self.settings.server_port}".encode()]
        allowed_client = client is not None and client[0] == "127.0.0.1"
        if self.settings.network_profile == "isolated-lab":
            allowed_hosts.append(f"platform-mock:{self.settings.server_port}".encode())
            try:
                allowed_client = allowed_client or (
                    client is not None
                    and ip_address(client[0]) in ip_network("10.42.0.0/24")
                )
            except ValueError:
                allowed_client = False
        hosts = [v for k, v in headers if k == b"host"]
        if (
            not allowed_client
            or len(hosts) != 1
            or hosts[0] not in allowed_hosts
            or any(
                k == b"forwarded" or k.startswith(b"x-forwarded-") or k == b"origin"
                for k, _ in headers
            )
        ):
            await reject(HTTPStatus.FORBIDDEN)
            return
        path = scope["path"]
        if not path.startswith("/health/"):
            token = (
                self.settings.control_token
                if path.startswith("/control/")
                else self.settings.service_token
            )
            values = [v for k, v in headers if k == b"authorization"]
            if len(values) != 1 or not compare_digest(
                values[0], f"Bearer {token.get_secret_value()}".encode()
            ):
                await reject(HTTPStatus.UNAUTHORIZED)
                return
        body = bytearray()
        try:
            async with asyncio.timeout(5):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > 16384:
                        await reject(HTTPStatus.CONTENT_TOO_LARGE)
                        return
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await reject(HTTPStatus.REQUEST_TIMEOUT)
            return
        delivered = False

        async def bounded_receive() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        async def no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": list(message.get("headers", []))
                    + [(b"cache-control", b"no-store")],
                }
            await send(message)

        await self.app(scope, bounded_receive, no_store)
