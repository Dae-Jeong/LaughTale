from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class ConversationKind(StrEnum):
    DM = "dm"


@dataclass(frozen=True, slots=True)
class Actor:
    user_id: UUID
    display_name: str


@dataclass(frozen=True, slots=True)
class ChatMessage:
    message_id: UUID
    conversation_id: UUID
    sender_id: UUID
    client_message_id: UUID
    seq: int
    text: str = field(repr=False)
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Room:
    conversation_id: UUID
    title: str
    head_seq: int


@dataclass(frozen=True, slots=True)
class History:
    messages: tuple[ChatMessage, ...]
    next_cursor: int
    snapshot_head_seq: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class Stored:
    message: ChatMessage
    replay: bool
