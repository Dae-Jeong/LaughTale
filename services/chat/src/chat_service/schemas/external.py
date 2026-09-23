from dataclasses import asdict
from typing import Literal, Self
from uuid import UUID

from platform_contracts.wire import DeliveryState, Profile
from pydantic import AwareDatetime

from chat_service.contracts.external import (
    ExternalHistory,
    ExternalMessageValue,
    ExternalRoom,
    SenderKind,
)
from chat_service.schemas.chat import HistoryMeta, MessageSeq, Seq, WireModel


class ExternalConversationData(WireModel):
    conversation_id: UUID
    connection_id: UUID
    profile: Profile
    external_conversation_id: str
    title: str
    head_seq: Seq

    @classmethod
    def from_internal(cls, room: ExternalRoom) -> Self:
        return cls.model_validate(asdict(room) | {"head_seq": str(room.head_seq)})


class ExternalMessageData(WireModel):
    message_id: UUID
    conversation_id: UUID
    sender_kind: SenderKind
    sender_id: str
    client_message_id: UUID | None
    external_message_id: str | None
    seq: MessageSeq
    text: str
    occurred_at: AwareDatetime
    received_at: AwareDatetime
    created_at: AwareDatetime
    operation_id: UUID | None
    delivery_state: DeliveryState | None
    external_sender_id: str | None
    effect_id: str | None

    @classmethod
    def from_internal(cls, message: ExternalMessageValue) -> Self:
        return cls.model_validate(asdict(message) | {"seq": str(message.seq)})


class ExternalHistoryResponse(WireModel):
    data: list[ExternalMessageData]
    meta: HistoryMeta

    @classmethod
    def from_internal(cls, history: ExternalHistory) -> Self:
        return cls(
            data=[
                ExternalMessageData.from_internal(message)
                for message in history.messages
            ],
            meta=HistoryMeta(
                next_cursor=str(history.next_cursor),
                snapshot_head_seq=str(history.snapshot_head_seq),
                has_more=history.has_more,
            ),
        )


class ExternalHead(WireModel):
    type: Literal["head"] = "head"
    conversation_id: UUID
    head_seq: Seq
