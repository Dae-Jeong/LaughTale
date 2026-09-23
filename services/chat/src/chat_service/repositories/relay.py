"""방의 미발행 선두와 lease를 PostgreSQL에서 원자적으로 소유합니다."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from chat_service.contracts.relay import RelayClaim, RelayError
from chat_service.models.chat import Message, MessageOutbox


class RelayRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def claim(self) -> RelayClaim | None:
        # Compute the unpublished head before eligibility checks: an in-flight
        # or retry-delayed head must still prevent later messages overtaking it.
        heads = (
            select(Message.conversation_id, func.min(Message.seq).label("seq"))
            .join(MessageOutbox, MessageOutbox.event_id == Message.id)
            .where(MessageOutbox.published_at.is_(None))
            .group_by(Message.conversation_id)
            .cte("unpublished_heads")
            .prefix_with("MATERIALIZED", dialect="postgresql")
        )
        row = (
            await self.session.execute(
                select(MessageOutbox, Message.conversation_id)
                .join(Message, Message.id == MessageOutbox.event_id)
                .join(
                    heads,
                    (heads.c.conversation_id == Message.conversation_id)
                    & (heads.c.seq == Message.seq),
                )
                .where(
                    MessageOutbox.published_at.is_(None),
                    or_(
                        MessageOutbox.lease_until.is_(None),
                        MessageOutbox.lease_until <= func.clock_timestamp(),
                    ),
                    or_(
                        MessageOutbox.next_attempt_at.is_(None),
                        MessageOutbox.next_attempt_at <= func.clock_timestamp(),
                    ),
                )
                .order_by(MessageOutbox.created_at, MessageOutbox.event_id)
                .limit(1)
                .with_for_update(skip_locked=True, of=MessageOutbox)
            )
        ).first()
        if row is None:
            return None
        outbox, conversation_id = row
        now = (await self.session.execute(select(func.clock_timestamp()))).scalar_one()
        outbox.claim_token = uuid4()
        outbox.lease_until = now + timedelta(seconds=30)
        outbox.attempts += 1
        await self.session.flush()
        return RelayClaim(
            outbox.event_id,
            conversation_id,
            outbox.claim_token,
            outbox.attempts,
            outbox.payload,
        )

    async def finish(
        self,
        claim: RelayClaim,
        *,
        retry_seconds: float | None = None,
        error_code: RelayError | None = None,
    ) -> bool:
        values = {
            "claim_token": None,
            "lease_until": None,
            "last_error_code": error_code.value if error_code is not None else None,
        }
        if retry_seconds is None:
            values.update(published_at=func.clock_timestamp(), next_attempt_at=None)
        else:
            values["next_attempt_at"] = func.clock_timestamp() + timedelta(
                seconds=retry_seconds
            )
        result = await self.session.execute(
            update(MessageOutbox)
            .where(
                MessageOutbox.event_id == claim.event_id,
                MessageOutbox.claim_token == claim.token,
                MessageOutbox.published_at.is_(None),
                MessageOutbox.lease_until > func.clock_timestamp(),
            )
            .values(**values)
            .returning(MessageOutbox.event_id)
        )
        return result.scalar_one_or_none() is not None
