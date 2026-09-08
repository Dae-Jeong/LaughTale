from dataclasses import FrozenInstanceError
from hashlib import sha256
from typing import cast

import pytest

from chat_service.domain.chat import (
    MessagePayload,
    ensure_same_payload,
    payload_fingerprint,
)
from chat_service.exceptions.chat import (
    IdempotencyConflictError,
    InvalidMessageTextError,
)


@pytest.mark.parametrize(
    "text",
    ["a", "  원문\n보존  ", "你好", "a" * 2000, "😀" * 2000, "e\u0301"],
)
def test_valid_text_is_preserved(text: str) -> None:
    payload = MessagePayload(text)
    assert payload.text == text
    assert payload.version == 1


@pytest.mark.parametrize(
    "text",
    ["", " \t\n", "\u3000", "a" * 2001, "😀" * 2001, "x\x00y", "x\ud800"],
)
def test_invalid_text_is_rejected_without_echoing_input(text: str) -> None:
    with pytest.raises(InvalidMessageTextError) as error:
        MessagePayload(text)
    if text:
        assert text not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize("value", [None, 12, True, b"text", ["text"]])
def test_non_string_is_not_coerced(value: object) -> None:
    # 의도적인 런타임 오용 시험이며 실제 입력 경계의 타입 검증을 대체하지 않습니다.
    with pytest.raises(InvalidMessageTextError):
        MessagePayload(cast(str, value))


def test_payload_is_immutable_and_repr_omits_text() -> None:
    payload = MessagePayload("private conversation")
    assert "private conversation" not in repr(payload)
    for attribute, value in (("text", "changed"), ("version", 2)):
        with pytest.raises(FrozenInstanceError):
            setattr(payload, attribute, value)


def test_fingerprint_uses_versioned_exact_utf8() -> None:
    payload = MessagePayload("  안녕😀\n")
    expected = sha256(b"chat.message.payload:1\x00" + "  안녕😀\n".encode()).hexdigest()
    assert payload_fingerprint(payload) == expected
    assert len(payload_fingerprint(payload)) == 64
    assert payload_fingerprint(MessagePayload(payload.text)) == expected
    assert payload_fingerprint(MessagePayload("안녕😀")) != expected


def test_identical_payload_is_accepted_without_mutation() -> None:
    stored = MessagePayload("message")
    incoming = MessagePayload("message")
    assert ensure_same_payload(stored=stored, incoming=incoming) is None
    assert stored == incoming


@pytest.mark.parametrize(
    "original,changed",
    [("hello", "hello "), ("hello", "Hello"), ("é", "e\u0301"), ("a\nb", "a\r\nb")],
)
def test_changed_payload_conflicts(original: str, changed: str) -> None:
    stored = MessagePayload(original)
    with pytest.raises(IdempotencyConflictError):
        ensure_same_payload(stored=stored, incoming=MessagePayload(changed))
    assert stored.text == original


def test_digest_collision_does_not_allow_different_content(monkeypatch) -> None:
    import chat_service.domain.chat as domain

    monkeypatch.setattr(domain, "payload_fingerprint", lambda payload: "same-digest")
    stored, incoming = MessagePayload("original"), MessagePayload("changed")
    assert domain.payload_fingerprint(stored) == domain.payload_fingerprint(incoming)
    with pytest.raises(IdempotencyConflictError):
        ensure_same_payload(stored=stored, incoming=incoming)
