"""메시지 내용 규칙입니다. 권한·멱등 키 조회·저장 순서는 업무와 DB가 소유합니다."""

from dataclasses import dataclass, field
from hashlib import sha256

from chat_service.exceptions.chat import (
    IdempotencyConflictError,
    InvalidMessageTextError,
)

MAX_MESSAGE_CODE_POINTS = 2000


@dataclass(frozen=True, slots=True)
class MessagePayload:
    """원문을 보존하는 v1 값입니다. repr에는 대화 내용을 노출하지 않습니다."""

    text: str = field(repr=False)
    version: int = field(default=1, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise InvalidMessageTextError("Message text must be a string")
        if not 1 <= len(self.text) <= MAX_MESSAGE_CODE_POINTS:
            raise InvalidMessageTextError("Message text length is out of range")
        if not self.text.strip():
            raise InvalidMessageTextError("Message text must not be blank")
        if "\x00" in self.text:
            raise InvalidMessageTextError("Message text must not contain NUL")
        try:
            self.text.encode("utf-8")
        except UnicodeEncodeError:
            raise InvalidMessageTextError("Message text must be valid UTF-8") from None


def payload_fingerprint(payload: MessagePayload) -> str:
    """내용 확인용 v1 digest이며 저장·권한·전달 완료의 증거는 아닙니다."""
    prefix = f"chat.message.payload:{payload.version}\x00".encode("ascii")
    return sha256(prefix + payload.text.encode("utf-8")).hexdigest()


def ensure_same_payload(*, stored: MessagePayload, incoming: MessagePayload) -> None:
    """호출자가 같은 키·권한을 확인한 뒤 사용하며 해시 일치만 신뢰하지 않습니다."""
    if stored.version != incoming.version or stored.text != incoming.text:
        raise IdempotencyConflictError("Message payload differs from stored payload")
