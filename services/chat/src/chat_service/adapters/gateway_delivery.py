"""허용 Pod IP에만 직접 전달하며 대상 인스턴스의 큐 수락을 검증합니다."""

import asyncio
from http import HTTPStatus

import httpx2

from chat_service.contracts.subscriptions import GatewayTarget
from chat_service.core.realtime_settings import validate_ip
from chat_service.schemas.chat import MessageCreated
from chat_service.schemas.gateway import GatewayAcceptance
from chat_service.schemas.responses import Success


class GatewayDeliveryClient:
    def __init__(
        self, client: httpx2.AsyncClient, token: str, *, allow_loopback: bool = False
    ) -> None:
        self.client, self.token, self.allow_loopback = client, token, allow_loopback
        self.slots = asyncio.Semaphore(4)

    async def send(self, target: GatewayTarget, event: MessageCreated) -> None:
        ip = validate_ip(target.ip, self.allow_loopback)
        async with self.slots, asyncio.timeout(1):
            async with self.client.stream(
                "POST",
                f"http://{ip}:18082/internal/events",
                headers={
                    "Authorization": "Bearer " + self.token,
                    "X-Gateway-Instance-ID": str(target.instance_id),
                },
                json={
                    "instance_id": str(target.instance_id),
                    "event": event.model_dump(mode="json"),
                },
            ) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 16384:
                        raise ValueError("GATEWAY_RESPONSE_LIMIT")
                if response.status_code != HTTPStatus.OK:
                    raise ValueError("GATEWAY_NOT_ACCEPTED")
                ack = Success[GatewayAcceptance].model_validate_json(raw).data
                if (
                    ack.instance_id != target.instance_id
                    or ack.event_id != event.event_id
                ):
                    raise ValueError("GATEWAY_ACK_IDENTITY_MISMATCH")
