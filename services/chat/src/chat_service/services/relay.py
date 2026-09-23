"""DB transaction 밖에서 발행하며 실패/취소 시 원장을 보존합니다."""

import asyncio
import random

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chat_service.contracts.relay import (
    EventPublisher,
    PublisherShutdownError,
    RelayError,
    RelayOutcome,
)
from chat_service.repositories.relay import RelayRepository


class OutboxRelay:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], publisher: EventPublisher
    ) -> None:
        self.factory = factory
        self.publisher = publisher

    async def once(self) -> RelayOutcome:
        async with asyncio.timeout(5), self.factory() as session, session.begin():
            claim = await RelayRepository(session).claim()
        if claim is None:
            return RelayOutcome.IDLE
        try:
            async with asyncio.timeout(10):
                await self.publisher.publish(claim)
        except asyncio.CancelledError:
            # Sending 여부가 불명확하므로 lease를 즉시 풀지 않습니다.
            raise
        except PublisherShutdownError:
            raise
        except Exception:
            backoff = min(10.0, 0.5 * 2 ** min(claim.attempt - 1, 5))
            delay = random.uniform(0.5, max(0.5, backoff))
            async with asyncio.timeout(5), self.factory() as session, session.begin():
                changed = await RelayRepository(session).finish(
                    claim, retry_seconds=delay, error_code=RelayError.PUBLISH_FAILED
                )
            return RelayOutcome.RETRY if changed else RelayOutcome.STALE
        async with asyncio.timeout(5), self.factory() as session, session.begin():
            changed = await RelayRepository(session).finish(claim)
        return RelayOutcome.PUBLISHED if changed else RelayOutcome.STALE
