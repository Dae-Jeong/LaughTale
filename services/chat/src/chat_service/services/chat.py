import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import monotonic
from uuid import UUID

from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chat_service.contracts.chat import Actor, History, Room, Stored
from chat_service.contracts.database import TransactionOutcome
from chat_service.core.database import acquire_primary_connection, primary_session
from chat_service.core.database_metrics import DatabaseMetrics
from chat_service.domain.chat import MessagePayload
from chat_service.exceptions.chat import (
    IdempotencyConflictError,
    InvalidCursorError,
    UnauthenticatedError,
)
from chat_service.exceptions.database import DatabaseBusy, DatabasePoolTimeout
from chat_service.repositories.chat import ChatRepository


class ChatService:
    """업무마다 Primary 트랜잭션을 소유하며 socket 수명에는 DB 세션을 묶지 않습니다."""

    def __init__(
        self, factory: async_sessionmaker[AsyncSession], metrics: DatabaseMetrics
    ) -> None:
        self.factory = factory
        self.metrics = metrics

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[ChatRepository]:
        started = monotonic()
        outcome = TransactionOutcome.FAILED
        body_error: BaseException | None = None
        try:
            async with (
                asyncio.timeout(10),
                primary_session(self.factory, self.metrics) as session,
                session.begin(),
            ):
                try:
                    await acquire_primary_connection(session, self.metrics)
                    yield ChatRepository(session)
                except BaseException as error:
                    body_error = error
                    raise
            outcome = TransactionOutcome.COMMITTED
        except BaseException as error:
            if error is body_error:
                outcome = TransactionOutcome.ROLLED_BACK
            if isinstance(error, PoolTimeout):
                raise DatabasePoolTimeout() from None
            if isinstance(error, TimeoutError):
                raise DatabaseBusy() from None
            if isinstance(error, DBAPIError) and getattr(
                error.orig, "sqlstate", None
            ) in {
                "55P03",
                "57014",
                "40P01",
                "40001",
            }:
                raise DatabaseBusy() from None
            raise
        finally:
            self.metrics.record_transaction(outcome, monotonic() - started)

    async def store(
        self, actor: UUID, room: UUID, key: UUID, payload: MessagePayload
    ) -> Stored:
        async with self.transaction() as repository:
            head = await repository.authorized_head(actor, room, lock=True)
            existing = await repository.find_message(actor, room, key)
            if existing is not None:
                # Repository가 저장 버전을 검증합니다. 과거 본문에 현재 입력 정책을 재적용하지 않습니다.
                if existing.text != payload.text:
                    raise IdempotencyConflictError(
                        "Message payload differs from stored payload"
                    )
                result = Stored(existing, True)
            else:
                if head >= 2**63 - 1:
                    raise InvalidCursorError()
                result = Stored(
                    await repository.append(actor, room, key, payload, head + 1), False
                )
        return result

    async def history(
        self, actor: UUID, room: UUID, after: int, snapshot: int | None, limit: int
    ) -> History:
        async with self.transaction() as repository:
            committed = await repository.authorized_head(actor, room)
            head = committed if snapshot is None else snapshot
            if not (0 <= after <= head <= committed and 1 <= limit <= 100):
                raise InvalidCursorError()
            messages = await repository.history(room, after, head, limit)
            cursor = messages[-1].seq if messages else after
            # 삭제 없는 S1에서 gap은 성공 응답으로 감추지 않습니다.
            if any(
                message.seq != after + index
                for index, message in enumerate(messages, start=1)
            ) or (cursor < head and len(messages) < limit):
                raise RuntimeError("History continuity violation")
            result = History(messages, cursor, head, cursor < head)
        return result

    async def rooms(self, actor: UUID) -> tuple[Room, ...]:
        async with self.transaction() as repository:
            return await repository.rooms(actor)

    async def head(self, actor: UUID, room: UUID) -> int:
        async with self.transaction() as repository:
            return await repository.authorized_head(actor, room)

    async def actor(self, user: UUID) -> Actor:
        async with self.transaction() as repository:
            actor = await repository.actor(user)
            if actor is None:
                raise UnauthenticatedError()
            return actor

    async def heads(
        self, actors: set[UUID], rooms: set[UUID]
    ) -> dict[tuple[UUID, UUID], int]:
        async with self.transaction() as repository:
            return await repository.heads(actors, rooms)
