"""내부 저장 이벤트입니다. HTTP·ORM에 의존하지 않습니다."""

import json

from chat_service.contracts.chat import ChatMessage


def message_created(message: ChatMessage) -> dict:
    event = {
        "type": "message.created",
        "event_id": str(message.message_id),
        "schema_version": 1,
        "message": {
            "message_id": str(message.message_id),
            "conversation_id": str(message.conversation_id),
            "sender_id": str(message.sender_id),
            "client_message_id": str(message.client_message_id),
            "seq": str(message.seq),
            "text": message.text,
            "created_at": message.created_at.isoformat(),
        },
    }
    if (
        len(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        > 16384
    ):
        raise ValueError("Message event exceeds byte limit")
    return event
