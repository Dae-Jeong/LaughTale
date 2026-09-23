import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from platform_contracts.wire import ConnectionSeed, InboundEvent

from chat_service.contracts.external import (
    ExternalHistory,
    ExternalMessageValue,
    ExternalRoom,
    ExternalStored,
)
from chat_service.core.contracts import Clock
from chat_service.domain.chat import MessagePayload, payload_fingerprint
from chat_service.exceptions.chat import (
    ConversationNotFoundError,
    IdempotencyConflictError,
    InvalidCursorError,
)
from chat_service.repositories.external import ExternalRepository
from chat_service.services.chat import ChatService


def event_fingerprint(event: InboundEvent, *, message: bool = False) -> str:
    body = event.model_dump(
        mode="json", exclude={"external_event_id"} if message else set()
    )
    return hashlib.sha256(
        json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


class ExternalService:
    """기존 Primary transaction·계측을 재사용하고 외부 업무 원자성을 소유합니다."""

    def __init__(
        self, database: ChatService, clock: Clock, *, connection_ids: frozenset[UUID]
    ) -> None:
        self.database = database
        self.clock = clock
        self.connection_ids = frozenset(connection_ids)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[ExternalRepository]:
        async with self.database.transaction() as repository:
            yield ExternalRepository(
                repository.session, connection_ids=self.connection_ids
            )

    async def seed(self, value: ConnectionSeed) -> UUID:
        async with self.transaction() as repository:
            return await repository.seed(value)

    async def receive(self, event: InboundEvent) -> ExternalStored:
        event_hash = event_fingerprint(event)
        message_hash = event_fingerprint(event, message=True)
        async with self.transaction() as repository:
            connection = await repository.connection(event.connection_id)
            if (connection.profile, connection.run_id) != (
                event.profile.value,
                event.run_id,
            ):
                raise IdempotencyConflictError()
            await repository.event_lock(event.connection_id, event.external_event_id)
            previous = await repository.event(
                event.connection_id, event.external_event_id
            )
            if previous is not None:
                if previous.payload_hash != event_hash:
                    raise IdempotencyConflictError()
                result = ExternalStored(
                    await repository.by_id(previous.message_id), True
                )
            else:
                room = await repository.external_room(
                    event.connection_id, event.external_conversation_id
                )
                existing = await repository.incoming_existing(
                    room.id, event.external_message_id
                )
                if existing is not None:
                    if existing.payload_hash != message_hash:
                        raise IdempotencyConflictError()
                    result = ExternalStored(await repository.by_id(existing.id), True)
                else:
                    if room.last_seq >= 2**63 - 1:
                        raise InvalidCursorError()
                    result = ExternalStored(
                        await repository.incoming(
                            room, event, message_hash, self.clock()
                        ),
                        False,
                    )
                await repository.record_event(
                    event, result.message.message_id, event_hash
                )
        return result

    async def send(
        self, actor: UUID, room: UUID, key: UUID, payload: MessagePayload
    ) -> ExternalStored:
        fingerprint = payload_fingerprint(payload)
        async with self.transaction() as repository:
            conversation = await repository.authorized_room(actor, room, lock=True)
            existing = await repository.outgoing_existing(room, actor, key)
            if existing is not None:
                if (
                    existing.payload_hash != fingerprint
                    or existing.text != payload.text
                ):
                    raise IdempotencyConflictError()
                result = ExternalStored(await repository.by_id(existing.id), True)
            else:
                if conversation.last_seq >= 2**63 - 1:
                    raise InvalidCursorError()
                result = ExternalStored(
                    await repository.outgoing(
                        conversation,
                        actor,
                        key,
                        payload.text,
                        fingerprint,
                        self.clock(),
                    ),
                    False,
                )
        return result

    async def rooms(self, actor: UUID) -> tuple[ExternalRoom, ...]:
        async with self.transaction() as repository:
            return await repository.rooms(actor)

    async def message(self, actor: UUID, room: UUID, id: UUID) -> ExternalMessageValue:
        async with self.transaction() as repository:
            await repository.authorized_room(actor, room)
            value = await repository.by_id(id)
            if value.conversation_id != room:
                raise ConversationNotFoundError()
            return value

    async def head(self, actor: UUID, room: UUID) -> int:
        async with self.transaction() as repository:
            return (await repository.authorized_room(actor, room)).last_seq

    async def history(
        self, actor: UUID, room: UUID, after: int, snapshot: int | None, limit: int
    ) -> ExternalHistory:
        async with self.transaction() as repository:
            conversation = await repository.authorized_room(actor, room)
            head = conversation.last_seq if snapshot is None else snapshot
            if not (0 <= after <= head <= conversation.last_seq and 1 <= limit <= 100):
                raise InvalidCursorError()
            messages = await repository.history(room, after, head, limit)
            cursor = messages[-1].seq if messages else after
            if any(
                item.seq != after + index for index, item in enumerate(messages, 1)
            ) or (cursor < head and len(messages) < limit):
                raise RuntimeError("External history continuity violation")
            return ExternalHistory(messages, cursor, head, cursor < head)
