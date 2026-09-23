"""HTTP·WS 제공자 계약입니다. 생성 산출물은 이 모델에서 내보냅니다."""

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
)

from chat_service.contracts.chat import (
    Actor,
    ChatMessage,
    ConversationKind,
    History,
    Room,
)
from chat_service.contracts.subscriptions import ResyncReason
from chat_service.domain.chat import MessagePayload
from chat_service.schemas.responses import ErrorCode

MAX_SEQ = 2**63 - 1


def validate_seq(value: str) -> str:
    if int(value) > MAX_SEQ:
        raise ValueError("Sequence exceeds bigint")
    return value


def seq_pattern(*, positive: bool = False) -> str:
    bound = str(MAX_SEQ)
    parts = [r"[1-9][0-9]{0,17}", bound]
    for index, digit in enumerate(bound):
        lower = 1 if index == 0 else 0
        if int(digit) > lower:
            parts.append(
                bound[:index] + f"[{lower}-{int(digit) - 1}]" + f"[0-9]{{{18 - index}}}"
            )
    return "^(" + "|".join(parts if positive else ["0", *parts]) + ")$"


Seq = Annotated[str, Field(pattern=seq_pattern()), AfterValidator(validate_seq)]
MessageSeq = Annotated[
    str, Field(pattern=seq_pattern(positive=True)), AfterValidator(validate_seq)
]


def utc_datetime(value: datetime) -> datetime:
    return value.astimezone(UTC)


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SessionRequest(WireModel):
    user: Literal["user_a", "user_b"]


class ActorData(WireModel):
    user_id: UUID
    display_name: str

    @classmethod
    def from_internal(cls, actor: Actor) -> Self:
        return cls(**asdict(actor))


class SendMessageRequest(WireModel):
    client_message_id: UUID
    text: Annotated[str, Field(min_length=1, max_length=2000)]

    def payload(self) -> MessagePayload:
        return MessagePayload(self.text)


class MessageData(WireModel):
    message_id: UUID
    conversation_id: UUID
    sender_id: UUID
    client_message_id: UUID
    seq: MessageSeq
    text: str
    created_at: Annotated[AwareDatetime, AfterValidator(utc_datetime)]

    @classmethod
    def from_internal(cls, message: ChatMessage) -> Self:
        return cls(**(asdict(message) | {"seq": str(message.seq)}))


class StoredMessageData(MessageData):
    state: Literal["stored"] = "stored"


class ConversationData(WireModel):
    conversation_id: UUID
    kind: Literal[ConversationKind.DM] = ConversationKind.DM
    title: str
    head_seq: Seq

    @classmethod
    def from_internal(cls, room: Room) -> Self:
        return cls(
            conversation_id=room.conversation_id,
            title=room.title,
            head_seq=str(room.head_seq),
        )


class HistoryMeta(WireModel):
    next_cursor: Seq
    snapshot_head_seq: Seq
    has_more: bool


class HistoryResponse(WireModel):
    data: list[MessageData]
    meta: HistoryMeta

    @classmethod
    def from_internal(cls, history: History) -> Self:
        return cls(
            data=[MessageData.from_internal(message) for message in history.messages],
            meta=HistoryMeta(
                next_cursor=str(history.next_cursor),
                snapshot_head_seq=str(history.snapshot_head_seq),
                has_more=history.has_more,
            ),
        )


class Subscribe(WireModel):
    type: Literal["subscribe"] = "subscribe"
    conversation_id: UUID


class Unsubscribe(WireModel):
    type: Literal["unsubscribe"] = "unsubscribe"
    conversation_id: UUID


class Subscribed(WireModel):
    type: Literal["subscribed"] = "subscribed"
    conversation_id: UUID
    head_seq: Seq
    protocol_version: Literal[1] = 1


class MessageCreated(WireModel):
    type: Literal["message.created"] = "message.created"
    event_id: UUID
    schema_version: Literal[1] = 1
    message: MessageData


class HeadItem(WireModel):
    conversation_id: UUID
    head_seq: Seq


class Heads(WireModel):
    type: Literal["heads"] = "heads"
    items: list[HeadItem]


class ResyncRequired(WireModel):
    type: Literal["resync_required"] = "resync_required"
    conversation_id: UUID
    reason: ResyncReason


class WSError(WireModel):
    type: Literal["error"] = "error"
    code: Literal[
        ErrorCode.UNAUTHENTICATED,
        ErrorCode.CONVERSATION_NOT_FOUND,
        ErrorCode.INVALID_INPUT,
        ErrorCode.HTTP_ERROR,
        ErrorCode.DATABASE_BUSY,
        ErrorCode.INTERNAL_ERROR,
    ]


ClientFrame = Annotated[Subscribe | Unsubscribe, Field(discriminator="type")]
ServerFrame = Annotated[
    Subscribed | MessageCreated | Heads | ResyncRequired | WSError,
    Field(discriminator="type"),
]
client_frame_adapter = TypeAdapter(ClientFrame)
server_frame_adapter = TypeAdapter(ServerFrame)
