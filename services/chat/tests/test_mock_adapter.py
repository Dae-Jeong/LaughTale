import asyncio
from uuid import uuid4

import httpx2
import pytest
from platform_contracts.wire import OutboundCommand, Profile

from chat_service.adapters.mock_platform import MockPlatform


@pytest.mark.parametrize(
    "case,state",
    [
        ("accepted", "accepted"),
        ("wrong_operation", "unknown"),
        ("rate_limit", "pending"),
        ("unavailable", "pending"),
        ("rejected", "rejected"),
        ("malformed", "unknown"),
        ("oversize", "unknown"),
        ("timeout", "unknown"),
    ],
)
def test_adapter_response_contract(case: str, state: str):
    async def scenario():
        command = OutboundCommand(
            run_id="test",
            profile=Profile.TELEGRAM,
            connection_id=uuid4(),
            external_conversation_id="room",
            outbound_operation_id=uuid4(),
            text="synthetic",
        )

        async def handler(request):
            assert request.headers["authorization"] == "Bearer synthetic"
            if case == "timeout":
                raise httpx2.ReadTimeout("synthetic")
            if case in {"rate_limit", "unavailable", "rejected"}:
                return httpx2.Response(
                    {"rate_limit": 429, "unavailable": 503, "rejected": 422}[case],
                    headers={"Retry-After": "20"},
                )
            if case in {"malformed", "oversize"}:
                return httpx2.Response(
                    201, content=b"invalid" if case == "malformed" else b"x" * 16385
                )
            return httpx2.Response(
                201,
                json={
                    "data": {
                        "effect_id": str(uuid4()),
                        "outbound_operation_id": str(
                            uuid4()
                            if case == "wrong_operation"
                            else command.outbound_operation_id
                        ),
                        "state": "accepted",
                    }
                },
            )

        async with httpx2.AsyncClient(
            base_url="http://127.0.0.1:18084",
            transport=httpx2.MockTransport(handler),
            headers={"Authorization": "Bearer synthetic"},
        ) as client:
            result = await MockPlatform(client).send_message(command)
            assert result.state == state
            assert result.retry_after <= 10

    asyncio.run(scenario())
