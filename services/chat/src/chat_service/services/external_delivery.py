import asyncio
import random
from uuid import UUID

from platform_contracts.wire import DeliveryState

from chat_service.contracts.delivery import (
    DeliveryClaim,
    DeliveryError,
    DeliveryResult,
    PlatformPort,
)
from chat_service.core.contracts import Clock
from chat_service.repositories.delivery import DeliveryRepository
from chat_service.services.chat import ChatService


class ExternalDelivery:
    def __init__(
        self,
        database: ChatService,
        port: PlatformPort,
        clock: Clock,
        *,
        connection_ids: frozenset[UUID],
    ) -> None:
        self.database = database
        self.port = port
        self.clock = clock
        self.connection_ids = frozenset(connection_ids)

    async def finish(self, claim: DeliveryClaim, result: DeliveryResult) -> bool:
        async with self.database.transaction() as repository:
            return await DeliveryRepository(repository.session).finish(
                claim, result, self.clock()
            )

    async def resolve_unknown(self, claim: DeliveryClaim) -> DeliveryResult:
        if claim.lookup_supported:
            effect = await self.port.lookup(claim.command)
            if effect is not None:
                return DeliveryResult(DeliveryState.ACCEPTED, str(effect.effect_id))
        # 조회 실패/404는 진행 중인 이전 요청의 부재를 증명하지 않습니다.
        if (
            claim.idempotency_supported
            and claim.attempt_count < 3
            and (self.clock() - claim.created_at).total_seconds() < 28
        ):
            return DeliveryResult(
                DeliveryState.PENDING,
                error_code=DeliveryError.RESPONSE_UNKNOWN,
                retry_after=1,
            )
        return DeliveryResult(
            DeliveryState.UNKNOWN, error_code=DeliveryError.RESPONSE_UNKNOWN
        )

    async def once(self) -> bool:
        async with self.database.transaction() as repository:
            claim = await DeliveryRepository(repository.session).claim(
                self.clock(), connection_ids=self.connection_ids
            )
        if claim is None:
            return False
        try:
            if claim.uncertain:
                result = await self.resolve_unknown(claim)
            elif (
                claim.attempt_count > 3
                or (self.clock() - claim.created_at).total_seconds() >= 30
            ):
                result = DeliveryResult(
                    DeliveryState.UNKNOWN
                    if claim.prior_unknown
                    else DeliveryState.REJECTED,
                    error_code=DeliveryError.RETRY_EXHAUSTED,
                )
            else:
                result = await self.port.send_message(claim.command)
                if result.state == DeliveryState.UNKNOWN:
                    result = await self.resolve_unknown(claim)
                elif result.state == DeliveryState.PENDING:
                    delay = max(
                        result.retry_after, 2 ** (claim.attempt_count - 1)
                    ) + random.uniform(0, 0.25)
                    if (
                        claim.attempt_count >= 3
                        or (self.clock() - claim.created_at).total_seconds() + delay
                        >= 30
                    ):
                        result = DeliveryResult(
                            DeliveryState.UNKNOWN
                            if claim.prior_unknown
                            else DeliveryState.REJECTED,
                            error_code=DeliveryError.RETRY_EXHAUSTED,
                        )
                    else:
                        result = DeliveryResult(
                            DeliveryState.PENDING,
                            error_code=DeliveryError.RESPONSE_UNKNOWN
                            if claim.prior_unknown
                            else result.error_code,
                            retry_after=delay,
                        )
                elif result.state == DeliveryState.REJECTED and claim.prior_unknown:
                    result = DeliveryResult(
                        DeliveryState.UNKNOWN, error_code=DeliveryError.RESPONSE_UNKNOWN
                    )
            await self.finish(claim, result)
        except asyncio.CancelledError:
            # sending lease를 보존합니다. 재시작 후에도 효과 부재로 오인하지 않습니다.
            raise
        except Exception:
            await self.finish(
                claim,
                DeliveryResult(
                    DeliveryState.UNKNOWN, error_code=DeliveryError.DELIVERY_ERROR
                ),
            )
        return True

    async def run(self) -> None:
        while True:
            try:
                worked = await self.once()
            except asyncio.CancelledError:
                raise
            except Exception:
                worked = False
            await asyncio.sleep(0.01 if worked else 0.25)
