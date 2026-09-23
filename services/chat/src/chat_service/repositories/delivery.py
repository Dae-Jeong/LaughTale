from datetime import datetime, timedelta
from uuid import UUID, uuid4

from platform_contracts.wire import DeliveryState, OutboundCommand, Profile
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from chat_service.contracts.delivery import DeliveryClaim, DeliveryError, DeliveryResult
from chat_service.models.external import (
    ExternalConnection,
    ExternalConversation,
    ExternalMessage,
    ExternalOutboundJob,
)


class DeliveryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def claim(
        self, now: datetime, *, connection_ids: frozenset[UUID]
    ) -> DeliveryClaim | None:
        if not connection_ids:
            return None
        job = await self.session.scalar(
            select(ExternalOutboundJob)
            .join(ExternalMessage, ExternalOutboundJob.message_id == ExternalMessage.id)
            .where(
                ExternalMessage.connection_id.in_(connection_ids),
                or_(
                    and_(
                        ExternalOutboundJob.state == DeliveryState.PENDING.value,
                        ExternalOutboundJob.next_attempt_at <= now,
                    ),
                    and_(
                        ExternalOutboundJob.state == DeliveryState.SENDING.value,
                        ExternalOutboundJob.lease_expires_at <= now,
                    ),
                ),
            )
            .order_by(ExternalOutboundJob.next_attempt_at, ExternalOutboundJob.id)
            .with_for_update(skip_locked=True, of=ExternalOutboundJob)
            .limit(1)
        )
        if job is None:
            return None
        message = await self.session.get(ExternalMessage, job.message_id)
        assert message is not None
        if message.operator_user_id is None:
            raise RuntimeError("Delivery requires operator message")
        room = await self.session.get(ExternalConversation, message.conversation_id)
        connection = await self.session.get(ExternalConnection, message.connection_id)
        assert room is not None and connection is not None
        uncertain = job.state == DeliveryState.SENDING.value
        job.state = DeliveryState.SENDING.value
        lease_token = uuid4()
        job.lease_token = lease_token
        job.lease_expires_at = now + timedelta(seconds=15)
        # 실제 전송 전 commit된 횟수는 crash 직후에 보수적인 시도 상한으로 사용합니다.
        job.attempt_count += 1
        await self.session.flush()
        return DeliveryClaim(
            OutboundCommand(
                run_id=connection.run_id,
                profile=Profile(connection.profile),
                connection_id=connection.id,
                external_conversation_id=room.external_conversation_id,
                outbound_operation_id=job.id,
                text=message.text,
            ),
            lease_token,
            job.attempt_count,
            job.created_at,
            uncertain,
            job.last_error_code == DeliveryError.RESPONSE_UNKNOWN.value,
            connection.idempotency_supported,
            connection.lookup_supported,
        )

    async def finish(
        self, claim: DeliveryClaim, result: DeliveryResult, now: datetime
    ) -> bool:
        changed = await self.session.scalar(
            update(ExternalOutboundJob)
            .where(
                ExternalOutboundJob.id == claim.command.outbound_operation_id,
                ExternalOutboundJob.state == DeliveryState.SENDING.value,
                ExternalOutboundJob.lease_token == claim.lease_token,
                ExternalOutboundJob.lease_expires_at > now,
            )
            .values(
                state=result.state.value,
                lease_token=None,
                lease_expires_at=None,
                effect_id=result.effect_id,
                last_error_code=result.error_code.value
                if result.error_code is not None
                else None,
                next_attempt_at=now + timedelta(seconds=result.retry_after),
            )
            .returning(ExternalOutboundJob.id)
        )
        return changed is not None
