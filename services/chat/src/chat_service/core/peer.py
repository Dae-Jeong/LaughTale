"""WebSocket peer's bounded delivery queue and close lifecycle."""

import asyncio
import hashlib
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum, auto
from uuid import UUID

from chat_service.schemas.chat import MessageCreated, ServerFrame, server_frame_adapter

MAX_QUEUE_FRAMES = 64
MAX_QUEUE_BYTES = 262144
MAX_SEEN_EVENTS = 256


class OfferResult(Enum):
    ACCEPTED = auto()
    DUPLICATE = auto()
    REJECTED = auto()


class CloseReason(Enum):
    QUEUE_OVERFLOW = auto()
    EVENT_CONFLICT = auto()
    AUTH_REVOKED = auto()
    REGISTRY_UNAVAILABLE = auto()
    RECOVERY_UNAVAILABLE = auto()
    SESSION_EXPIRED = auto()
    SERVER_SHUTDOWN = auto()
    DELIVERY_FAILED = auto()


@dataclass(eq=False)
class Peer:
    actor_id: UUID | None = None
    token: str | None = field(default=None, repr=False)
    rooms: set[UUID] = field(default_factory=set)
    session_valid_until: float = 0.0
    outgoing_frames: asyncio.Queue[str] = field(
        default_factory=lambda: asyncio.Queue(MAX_QUEUE_FRAMES), init=False, repr=False
    )
    close_signal: asyncio.Event = field(
        default_factory=asyncio.Event, init=False, repr=False
    )
    closed_signal: asyncio.Event = field(
        default_factory=asyncio.Event, init=False, repr=False
    )
    seen_event_digests: OrderedDict[UUID, bytes] = field(
        default_factory=OrderedDict, init=False, repr=False
    )
    byte_count: int = field(default=0, init=False, repr=False)
    reason: CloseReason | None = field(default=None, init=False, repr=False)
    closed: bool = field(default=False, init=False, repr=False)

    @property
    def is_closing(self) -> bool:
        return self.close_signal.is_set()

    @property
    def close_reason(self) -> CloseReason | None:
        return self.reason

    @property
    def queued_bytes(self) -> int:
        return self.byte_count

    @property
    def queued_frames(self) -> int:
        return self.outgoing_frames.qsize()

    @property
    def seen_count(self) -> int:
        return len(self.seen_event_digests)

    def offer(self, frame: ServerFrame) -> OfferResult:
        if self.closed or self.is_closing:
            return OfferResult.REJECTED
        encoded = server_frame_adapter.dump_json(frame).decode()
        digest = hashlib.sha256(encoded.encode()).digest()
        if (
            isinstance(frame, MessageCreated)
            and frame.event_id in self.seen_event_digests
        ):
            if self.seen_event_digests[frame.event_id] == digest:
                return OfferResult.DUPLICATE
            self.request_close(CloseReason.EVENT_CONFLICT)
            return OfferResult.REJECTED
        size = len(encoded.encode())
        if self.outgoing_frames.full() or self.byte_count + size > MAX_QUEUE_BYTES:
            self.request_close(CloseReason.QUEUE_OVERFLOW)
            return OfferResult.REJECTED
        self.outgoing_frames.put_nowait(encoded)
        self.byte_count += size
        if isinstance(frame, MessageCreated):
            self.seen_event_digests[frame.event_id] = digest
            if len(self.seen_event_digests) > MAX_SEEN_EVENTS:
                self.seen_event_digests.popitem(last=False)
        return OfferResult.ACCEPTED

    async def next_frame(self) -> str:
        encoded = await self.outgoing_frames.get()
        self.byte_count -= len(encoded.encode())
        return encoded

    def request_close(self, reason: CloseReason) -> None:
        if self.closed:
            return
        if self.reason is None or reason is CloseReason.AUTH_REVOKED:
            self.reason = reason
        self.close_signal.set()

    async def wait_close_requested(self) -> CloseReason:
        await self.close_signal.wait()
        assert self.reason is not None
        return self.reason

    def mark_closed(self) -> None:
        self.closed = True
        self.closed_signal.set()

    async def wait_closed(self) -> None:
        await self.closed_signal.wait()
