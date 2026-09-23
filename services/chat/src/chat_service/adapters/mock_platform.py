import asyncio
import json
from http import HTTPStatus

import httpx2
from platform_contracts.wire import AcceptedEffect, DeliveryState, OutboundCommand

from chat_service.contracts.delivery import DeliveryError, DeliveryResult


class MockPlatform:
    """설정된 단일 mock에만 접속하며 SDK 재시도와 redirect를 사용하지 않습니다."""

    def __init__(self, client: httpx2.AsyncClient) -> None:
        self.client = client

    async def request(
        self, method: str, path: str, command: OutboundCommand
    ) -> tuple[int, dict[str, str], bytes]:
        async with asyncio.timeout(2):
            async with self.client.stream(
                method,
                path,
                json=command.model_dump(mode="json") if method == "POST" else None,
                params={"run_id": command.run_id} if method == "GET" else None,
            ) as response:
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 16384:
                        raise ValueError("Oversized provider response")
                return response.status_code, dict(response.headers), bytes(data)

    async def send_message(self, command: OutboundCommand) -> DeliveryResult:
        try:
            status, headers, body = await self.request(
                "POST", "/mock/v1/messages", command
            )
            if status in {HTTPStatus.OK, HTTPStatus.CREATED}:
                effect = AcceptedEffect.model_validate(json.loads(body)["data"])
                if effect.outbound_operation_id != command.outbound_operation_id:
                    raise ValueError("Unexpected operation response")
                return DeliveryResult(DeliveryState.ACCEPTED, str(effect.effect_id))
            if status in {HTTPStatus.TOO_MANY_REQUESTS, HTTPStatus.SERVICE_UNAVAILABLE}:
                try:
                    delay = min(10, max(0, float(headers.get("retry-after", "0"))))
                except ValueError:
                    delay = 0
                return DeliveryResult(
                    DeliveryState.PENDING,
                    error_code=DeliveryError.PROVIDER_UNAVAILABLE,
                    retry_after=delay,
                )
            if status in {
                HTTPStatus.BAD_REQUEST,
                HTTPStatus.UNAUTHORIZED,
                HTTPStatus.FORBIDDEN,
                HTTPStatus.NOT_FOUND,
                HTTPStatus.CONFLICT,
                HTTPStatus.UNPROCESSABLE_CONTENT,
            }:
                return DeliveryResult(
                    DeliveryState.REJECTED, error_code=DeliveryError.PROVIDER_REJECTED
                )
            return DeliveryResult(
                DeliveryState.UNKNOWN, error_code=DeliveryError.UNEXPECTED_RESPONSE
            )
        except httpx2.HTTPError, TimeoutError, ValueError, KeyError, TypeError:
            return DeliveryResult(
                DeliveryState.UNKNOWN, error_code=DeliveryError.RESPONSE_UNKNOWN
            )

    async def lookup(self, command: OutboundCommand) -> AcceptedEffect | None:
        try:
            status, _, body = await self.request(
                "GET", f"/mock/v1/operations/{command.outbound_operation_id}", command
            )
            if status != HTTPStatus.OK:
                return None
            effect = AcceptedEffect.model_validate(json.loads(body)["data"])
            if effect.outbound_operation_id != command.outbound_operation_id:
                return None
            return effect
        except httpx2.HTTPError, TimeoutError, ValueError, KeyError, TypeError:
            return None
