"""DB 연결 없이 제공자 계약과 합성 fixture를 생성·차이 검사합니다."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from chat_service.bootstrap.app import create_app
from chat_service.contracts.subscriptions import ResyncReason
from chat_service.core.settings import Settings
from chat_service.routers.external import router as external_router
from chat_service.schemas.chat import (
    ActorData,
    ConversationData,
    HeadItem,
    Heads,
    HistoryMeta,
    HistoryResponse,
    MessageCreated,
    MessageData,
    ResyncRequired,
    SessionRequest,
    StoredMessageData,
    Subscribe,
    Subscribed,
    Unsubscribe,
    WSError,
    client_frame_adapter,
    server_frame_adapter,
)
from chat_service.schemas.external import ExternalHead
from chat_service.schemas.responses import ErrorCode, Success


def artifacts() -> dict[str, object]:
    actor = UUID("00000000-0000-4000-8000-000000000001")
    room = UUID("00000000-0000-4000-8000-000000000010")
    message = MessageData(
        message_id=UUID("00000000-0000-4000-8000-000000000100"),
        conversation_id=room,
        sender_id=actor,
        client_message_id=UUID("00000000-0000-4000-8000-000000000200"),
        seq="1",
        text="안녕하세요 👋",
        created_at=datetime(2026, 9, 8, tzinfo=UTC),
    )
    values = {
        "session_request": SessionRequest(user="user_a"),
        "session": Success(data=ActorData(user_id=actor, display_name="User A")),
        "conversations": Success(
            data=[ConversationData(conversation_id=room, title="User B", head_seq="1")]
        ),
        "stored": Success(data=StoredMessageData(**message.model_dump())),
        "history": HistoryResponse(
            data=[message],
            meta=HistoryMeta(next_cursor="1", snapshot_head_seq="1", has_more=False),
        ),
        "subscribe": Subscribe(conversation_id=room),
        "unsubscribe": Unsubscribe(conversation_id=room),
        "subscribed": Subscribed(conversation_id=room, head_seq="1"),
        "message_created": MessageCreated(event_id=message.message_id, message=message),
        "heads": Heads(items=[HeadItem(conversation_id=room, head_seq="1")]),
        "resync_required": ResyncRequired(
            conversation_id=room, reason=ResyncReason.QUEUE_OVERFLOW
        ),
        "error": WSError(code=ErrorCode.UNAUTHENTICATED),
    }
    app = create_app(Settings(_env_file=None, dev_sessions_enabled=True))
    app.include_router(external_router)
    return {
        "openapi.json": app.openapi(),
        "external-ws-client.schema.json": Subscribe.model_json_schema(),
        "external-ws-server.schema.json": ExternalHead.model_json_schema(),
        "ws-client.schema.json": client_frame_adapter.json_schema(),
        "ws-server.schema.json": server_frame_adapter.json_schema(),
        "fixtures.json": {
            key: value.model_dump(mode="json") for key, value in values.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[4] / "contracts/chat",
    )
    args = parser.parse_args()
    for name, value in artifacts().items():
        content = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        path = args.output / name
        if args.check:
            if not path.exists() or path.read_text() != content:
                raise SystemExit(f"Contract drift: {name}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)


if __name__ == "__main__":
    main()
