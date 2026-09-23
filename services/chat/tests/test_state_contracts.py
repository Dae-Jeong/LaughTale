import json
from enum import StrEnum
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from chat_service.contracts.chat import ConversationKind
from chat_service.contracts.database import AuthOutcome, AuthPhase
from chat_service.contracts.delivery import DeliveryError
from chat_service.contracts.external import SenderKind
from chat_service.contracts.relay import RelayError, RelayOutcome
from chat_service.contracts.subscriptions import (
    GatewayOutcome,
    ReconcileOutcome,
    ResyncReason,
)
from chat_service.core.contracts import HealthStatus
from chat_service.schemas.chat import WSError
from chat_service.schemas.gateway import GatewayAcceptance
from chat_service.schemas.responses import ErrorCode


@pytest.mark.parametrize(
    "enum",
    [
        ConversationKind,
        AuthOutcome,
        AuthPhase,
        DeliveryError,
        SenderKind,
        RelayError,
        RelayOutcome,
        GatewayOutcome,
        ReconcileOutcome,
        ResyncReason,
        HealthStatus,
    ],
)
def test_enum_wire_values_round_trip_without_implicit_fallback(enum: type[StrEnum]):
    adapter = TypeAdapter(enum)
    for member in enum:
        assert adapter.validate_json(json.dumps(member.value)) is member
        assert adapter.dump_json(member) == json.dumps(member.value).encode()
    with pytest.raises(ValidationError):
        adapter.validate_json('"synthetic-invalid-state"')


def test_gateway_ack_contains_enum_but_keeps_wire_outcome():
    for outcome in GatewayOutcome:
        ack = GatewayAcceptance.model_validate(
            {
                "instance_id": str(uuid4()),
                "event_id": str(uuid4()),
                "outcome": outcome.value,
            }
        )
        assert ack.outcome is outcome
        assert ack.model_dump(mode="json")["outcome"] == outcome.value


def test_ws_error_reuses_http_enum_without_expanding_allowed_codes():
    allowed = {
        ErrorCode.UNAUTHENTICATED,
        ErrorCode.CONVERSATION_NOT_FOUND,
        ErrorCode.INVALID_INPUT,
        ErrorCode.HTTP_ERROR,
        ErrorCode.DATABASE_BUSY,
        ErrorCode.INTERNAL_ERROR,
    }
    for code in ErrorCode:
        raw = json.dumps({"type": "error", "code": code.value})
        if code in allowed:
            frame = WSError.model_validate_json(raw)
            assert frame.code is code
            assert frame.model_dump(mode="json") == {
                "type": "error",
                "code": code.value,
            }
        else:
            with pytest.raises(ValidationError):
                WSError.model_validate_json(raw)
