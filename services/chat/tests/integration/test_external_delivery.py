import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from platform_contracts.wire import AcceptedEffect, DeliveryState
from sqlalchemy import select, update
from tests.integration.test_chat_service import A, service_database
from tests.integration.test_external_service import seed_input

from chat_service.contracts.delivery import DeliveryError, DeliveryResult
from chat_service.domain.chat import MessagePayload
from chat_service.models.external import ExternalOutboundJob
from chat_service.repositories.delivery import DeliveryRepository
from chat_service.services.external import ExternalService
from chat_service.services.external_delivery import ExternalDelivery

pytestmark = pytest.mark.postgres


def test_delivery_enum_round_trip_preserves_text_storage(postgres_url: str):
    async def scenario():
        async with service_database(postgres_url) as database:
            now = datetime.now(UTC)
            seed = seed_input()
            service = ExternalService(
                database, lambda: now, connection_ids=frozenset({seed.connection_id})
            )
            room = await service.seed(seed)
            stored = await service.send(
                A, room, uuid4(), MessagePayload("synthetic enum storage")
            )
            async with database.transaction() as repository:
                delivery = DeliveryRepository(repository.session)
                claim = await delivery.claim(
                    now, connection_ids=frozenset({seed.connection_id})
                )
                assert claim is not None
            async with database.transaction() as repository:
                assert await DeliveryRepository(repository.session).finish(
                    claim,
                    DeliveryResult(
                        DeliveryState.UNKNOWN, error_code=DeliveryError.RESPONSE_UNKNOWN
                    ),
                    now,
                )
            async with database.transaction() as repository:
                job = await repository.session.get(
                    ExternalOutboundJob, claim.command.outbound_operation_id
                )
                assert job is not None
                assert type(job.state) is str and job.state == "unknown"
                assert (
                    type(job.last_error_code) is str
                    and job.last_error_code == "RESPONSE_UNKNOWN"
                )
            message = await service.message(A, room, stored.message.message_id)
            assert message.delivery_state is DeliveryState.UNKNOWN

    asyncio.run(scenario())


class Port:
    def __init__(self, state=DeliveryState.ACCEPTED, lookup=True):
        self.state, self.lookup_enabled = state, lookup
        self.calls = 0
        self.effects = {}
        self.block = None

    async def send_message(self, command):
        self.calls += 1
        if self.state in {DeliveryState.ACCEPTED, DeliveryState.UNKNOWN}:
            self.effects.setdefault(
                command.outbound_operation_id,
                AcceptedEffect(
                    effect_id=uuid4(),
                    outbound_operation_id=command.outbound_operation_id,
                ),
            )
        if self.block is not None:
            self.block.set()
            await asyncio.Event().wait()
        return DeliveryResult(
            self.state,
            str(self.effects[command.outbound_operation_id].effect_id)
            if self.state == DeliveryState.ACCEPTED
            else None,
        )

    async def lookup(self, command):
        return (
            self.effects.get(command.outbound_operation_id)
            if self.lookup_enabled
            else None
        )


@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "unknown_lookup",
        "unknown_no_capability",
        "retry_limit",
        "cancel_and_resume",
        "lease_fencing",
    ],
)
def test_durable_delivery(postgres_url: str, mode: str):
    async def scenario():
        async with service_database(postgres_url) as database:
            now = [datetime.now(UTC)]

            def clock():
                return now[0]

            seed = seed_input()
            service = ExternalService(
                database, clock, connection_ids=frozenset({seed.connection_id})
            )
            if mode == "unknown_no_capability":
                seed = type(seed).model_validate(
                    seed.model_dump()
                    | {"idempotency_supported": False, "lookup_supported": False}
                )
            room = await service.seed(seed)
            stored = await service.send(A, room, uuid4(), MessagePayload("reply"))
            state = (
                DeliveryState.UNKNOWN
                if mode.startswith("unknown")
                else DeliveryState.PENDING
                if mode == "retry_limit"
                else DeliveryState.ACCEPTED
            )
            port = Port(state, mode != "unknown_no_capability")
            delivery = ExternalDelivery(
                database, port, clock, connection_ids=frozenset({seed.connection_id})
            )
            if mode == "cancel_and_resume":
                port.block = asyncio.Event()
                task = asyncio.create_task(delivery.once())
                await port.block.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                now[0] += timedelta(seconds=16)
                port.block = None
                assert await delivery.once()
                assert port.calls == 1
            elif mode == "lease_fencing":
                async with database.transaction() as repo:
                    old = await DeliveryRepository(repo.session).claim(
                        clock(), connection_ids=frozenset({seed.connection_id})
                    )
                assert old is not None
                now[0] += timedelta(seconds=16)
                async with database.transaction() as repo:
                    new = await DeliveryRepository(repo.session).claim(
                        clock(), connection_ids=frozenset({seed.connection_id})
                    )
                assert new is not None and old.lease_token != new.lease_token
                assert not await delivery.finish(
                    old, DeliveryResult(DeliveryState.ACCEPTED, "stale")
                )
                assert await delivery.finish(
                    new, DeliveryResult(DeliveryState.ACCEPTED, "current")
                )
            else:
                assert await delivery.once()
                if mode == "retry_limit":
                    for _ in range(4):
                        now[0] += timedelta(seconds=5)
                        await delivery.once()
                    assert port.calls == 3
            message = await service.message(A, room, stored.message.message_id)
            expected = (
                "unknown"
                if mode == "unknown_no_capability"
                else "rejected"
                if mode == "retry_limit"
                else "accepted"
            )
            assert message.delivery_state == expected
            if expected == "accepted":
                assert message.effect_id is not None
            async with database.factory() as session:
                job = (await session.scalars(select(ExternalOutboundJob))).one()
                assert job.lease_token is None and job.lease_expires_at is None

    asyncio.run(scenario())


@pytest.mark.parametrize("other_state", ["pending", "sending"])
def test_connection_scoped_workers_cannot_claim_other_runtime_jobs(
    postgres_url: str, other_state: str
):
    async def scenario():
        async with service_database(postgres_url) as database:
            now = datetime.now(UTC)
            seed_a, seed_b = seed_input(), seed_input()
            service = ExternalService(
                database,
                lambda: now,
                connection_ids=frozenset({seed_a.connection_id, seed_b.connection_id}),
            )
            room_a, room_b = await service.seed(seed_a), await service.seed(seed_b)
            message_a = await service.send(
                A, room_a, uuid4(), MessagePayload("runtime A")
            )
            message_b = await service.send(
                A, room_b, uuid4(), MessagePayload("runtime B")
            )
            port_a, port_b = Port(), Port()
            assert message_b.message.operation_id is not None
            if other_state == "sending":
                async with database.factory() as session, session.begin():
                    await session.execute(
                        update(ExternalOutboundJob)
                        .where(ExternalOutboundJob.id == message_b.message.operation_id)
                        .values(
                            state="sending",
                            attempt_count=1,
                            lease_token=uuid4(),
                            lease_expires_at=now - timedelta(seconds=1),
                        )
                    )
                port_b.effects[message_b.message.operation_id] = AcceptedEffect(
                    effect_id=uuid4(),
                    outbound_operation_id=message_b.message.operation_id,
                )
            worker_a = ExternalDelivery(
                database,
                port_a,
                lambda: now,
                connection_ids=frozenset({seed_a.connection_id}),
            )
            worker_b = ExternalDelivery(
                database,
                port_b,
                lambda: now,
                connection_ids=frozenset({seed_b.connection_id}),
            )
            empty = ExternalDelivery(
                database, port_a, lambda: now, connection_ids=frozenset()
            )
            assert not await empty.once()
            assert await worker_a.once()
            assert not await worker_a.once()
            assert set(port_a.effects) == {message_a.message.operation_id}
            async with database.factory() as session:
                job_b = await session.get(
                    ExternalOutboundJob, message_b.message.operation_id
                )
                assert job_b is not None and job_b.state == other_state
                assert job_b.attempt_count == (1 if other_state == "sending" else 0)
            assert await worker_b.once()
            assert set(port_b.effects) == {message_b.message.operation_id}
            assert port_b.calls == (0 if other_state == "sending" else 1)
            assert (
                await service.message(A, room_a, message_a.message.message_id)
            ).delivery_state == "accepted"
            assert (
                await service.message(A, room_b, message_b.message.message_id)
            ).delivery_state == "accepted"

    asyncio.run(scenario())


def test_two_workers_cannot_claim_one_job(postgres_url: str):
    async def scenario():
        async with service_database(postgres_url) as database:
            seed = seed_input()
            service = ExternalService(
                database,
                lambda: datetime.now(UTC),
                connection_ids=frozenset({seed.connection_id}),
            )
            room = await service.seed(seed)
            await service.send(A, room, uuid4(), MessagePayload("reply"))
            port = Port()
            worker = ExternalDelivery(
                database,
                port,
                lambda: datetime.now(UTC),
                connection_ids=frozenset({seed.connection_id}),
            )
            results = await asyncio.gather(worker.once(), worker.once())
            assert sum(results) == 1 and port.calls == 1

    asyncio.run(scenario())


def test_late_effect_after_lookup_miss_remains_unknown_without_resend(
    postgres_url: str,
):
    """delay-before timeout의 결정 경계를 제어하며 실제 provider 성능을 가정하지 않습니다."""

    async def scenario():
        async with service_database(postgres_url) as database:
            original = seed_input()
            service = ExternalService(
                database,
                lambda: datetime.now(UTC),
                connection_ids=frozenset({original.connection_id}),
            )
            seed = type(original).model_validate(
                original.model_dump()
                | {"idempotency_supported": False, "lookup_supported": True}
            )
            room = await service.seed(seed)
            stored = await service.send(A, room, uuid4(), MessagePayload("late effect"))
            release = asyncio.Event()
            effects = []
            pending = []

            class LatePort:
                calls = 0
                lookups = 0

                async def send_message(self, command):
                    self.calls += 1

                    async def complete_later():
                        await release.wait()
                        effects.append(
                            AcceptedEffect(
                                effect_id=uuid4(),
                                outbound_operation_id=command.outbound_operation_id,
                            )
                        )

                    pending.append(asyncio.create_task(complete_later()))
                    # HTTP 대역이 timeout을 관찰했으나 provider 작업은 아직 진행 중입니다.
                    return DeliveryResult(DeliveryState.UNKNOWN)

                async def lookup(self, command):
                    self.lookups += 1
                    return effects[0] if effects else None

            port = LatePort()
            worker = ExternalDelivery(
                database,
                port,
                lambda: datetime.now(UTC),
                connection_ids=frozenset({seed.connection_id}),
            )
            try:
                assert await worker.once()
                assert not effects and port.calls == 1 and port.lookups == 1
                assert (
                    await service.message(A, room, stored.message.message_id)
                ).delivery_state == "unknown"
                release.set()
                await asyncio.gather(*pending)
                assert len(effects) == 1
                assert not await worker.once()
                message = await service.message(A, room, stored.message.message_id)
                assert message.delivery_state == "unknown" and message.effect_id is None
                assert port.calls == 1 and port.lookups == 1
            finally:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run(scenario())
