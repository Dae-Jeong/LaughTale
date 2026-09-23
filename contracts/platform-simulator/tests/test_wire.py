from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from platform_contracts.wire import InboundEvent, Profile, RunConfig


def event(**changes):
    values = {
        "run_id": "run-1",
        "profile": "telegram",
        "connection_id": UUID(int=1),
        "external_conversation_id": "room-1",
        "external_event_id": "event-1",
        "external_message_id": "message-1",
        "external_sender_id": "customer-1",
        "occurred_at": datetime(2026, 9, 8, tzinfo=UTC),
        "text": "합성 메시지입니다.",
    }
    return InboundEvent.model_validate(values | changes)


@pytest.mark.parametrize("profile", list(Profile))
def test_profiles_share_exact_wire(profile):
    value = event(profile=profile)
    assert InboundEvent.model_validate_json(value.model_dump_json()) == value


@pytest.mark.parametrize("text", ["", " \n", "a\x00b", "\ud800", "a" * 2001])
def test_invalid_text_rejected(text):
    with pytest.raises(ValidationError):
        event(text=text)


def test_preserve_text_no_silent_normalization():
    assert event(text="  한글\n ").text == "  한글\n "


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": 2},
        {"extra": "unknown"},
        {"profile": "real-provider"},
        {"external_event_id": "../bad"},
        {"occurred_at": "2026-09-08T00:00:00"},
        {"connection_id": "invalid"},
    ],
)
def test_invalid_envelopes_rejected(changes):
    with pytest.raises(ValidationError):
        event(**changes)


def test_run_caps_and_capabilities():
    value = RunConfig(run_id="run", idempotency_supported=False, lookup_supported=False)
    assert not value.idempotency_supported and not value.lookup_supported
    for change in [
        {"max_events": 10001},
        {"delay_seconds": 11},
        {"fault_attempts": 6},
    ]:
        with pytest.raises(ValidationError):
            RunConfig(run_id="run", **change)
