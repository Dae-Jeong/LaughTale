"""partition마다 ACK 이전 offset commit을 금지하는 분산 전달입니다."""

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from aiokafka import ConsumerRebalanceListener, ConsumerRecord, TopicPartition
from aiokafka.errors import NoOffsetForPartitionError

from chat_service.contracts.subscriptions import GatewayTarget, SubscriptionRegistry
from chat_service.schemas.chat import MessageCreated

TOPIC = "chat.message-created.v1"
GROUP = "chat-fanout-v1"


class Consumer(Protocol):
    async def committed(self, partition: TopicPartition, /) -> int | None: ...
    async def beginning_offsets(
        self, partitions: Sequence[TopicPartition], /
    ) -> dict[TopicPartition, int]: ...
    async def getmany(
        self, *, timeout_ms: int, max_records: int
    ) -> dict[TopicPartition, list[ConsumerRecord[bytes | None, bytes | None]]]: ...
    async def commit(self, offsets: dict[TopicPartition, int], /) -> None: ...
    def pause(self, *partitions: TopicPartition) -> None: ...
    def resume(self, *partitions: TopicPartition) -> None: ...
    def seek(self, partition: TopicPartition, offset: int, /) -> None: ...


@dataclass
class PartitionState:
    ownership_token: object | None
    assignment_start_offset: int
    task: asyncio.Task[None] | None = None


def decode_event(value: bytes, key: bytes) -> MessageCreated:
    if len(value) > 16384:
        raise ValueError("EVENT_LIMIT")
    raw = json.loads(value)
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version") != 1
        or raw.get("type") != "message.created"
    ):
        raise ValueError("UNSUPPORTED_EVENT_SCHEMA")
    event = MessageCreated.model_validate(raw)
    if (
        key != str(event.message.conversation_id).encode()
        or event.event_id != event.message.message_id
    ):
        raise ValueError("EVENT_IDENTITY_MISMATCH")
    return event


class FanoutDelivery:
    def __init__(
        self,
        registry: SubscriptionRegistry,
        send: Callable[[GatewayTarget, MessageCreated], Awaitable[None]],
    ) -> None:
        self.registry, self.send = registry, send

    async def deliver(self, event: MessageCreated) -> None:
        # 성공 표시는 이 시도 안에서만 유지합니다. 재시도 시 모든 살아 있는 대상에 재전달합니다.
        async with asyncio.timeout(5):
            accepted: set[GatewayTarget] = set()
            for _ in range(4):
                targets = set(
                    await self.registry.targets(event.message.conversation_id)
                )
                missing = targets - accepted
                if not missing:
                    return
                results = await asyncio.gather(
                    *(self.send(target, event) for target in missing),
                    return_exceptions=True,
                )
                for target, result in zip(missing, results, strict=True):
                    if not isinstance(result, BaseException):
                        accepted.add(target)
                current = set(
                    await self.registry.targets(event.message.conversation_id)
                )
                # 실패 대상 TTL 만료와 새 구독 대상을 함께 다시 확인합니다.
                if current <= accepted:
                    return
                if any(isinstance(result, BaseException) for result in results):
                    raise RuntimeError("LIVE_GATEWAY_UNCONFIRMED")
            raise RuntimeError("ROUTING_CHURN_RETRY")


class PartitionFanout(ConsumerRebalanceListener):
    def __init__(self, consumer: Consumer, delivery: FanoutDelivery) -> None:
        self.consumer, self.delivery = consumer, delivery
        self.states: dict[TopicPartition, PartitionState] = {}
        self.failed = False
        self.stopping = False
        self.assignment_ready = asyncio.Event()
        self.assignment_failed = False
        self.assignment_generation = object()

    async def on_partitions_revoked(self, revoked: Iterable[TopicPartition]) -> None:
        self.assignment_ready.clear()
        self.assignment_generation = object()
        captured = [
            (partition, state)
            for partition in revoked
            if (state := self.states.get(partition)) is not None
        ]
        pending: list[asyncio.Task[None]] = []
        for _, state in captured:
            state.ownership_token = None
            if state.task is not None:
                state.task.cancel()
                pending.append(state.task)
        await asyncio.gather(*pending, return_exceptions=True)
        for partition, state in captured:
            if self.states.get(partition) is state:
                self.states.pop(partition)

    async def on_partitions_assigned(self, assigned: Iterable[TopicPartition]) -> None:
        assigned = tuple(assigned)
        self.assignment_ready.clear()
        self.assignment_generation = object()
        self.assignment_failed = False
        if assigned:
            self.consumer.pause(*assigned)
        try:
            for partition in assigned:
                committed = await self.consumer.committed(partition)
                if committed is None:
                    beginnings = await self.consumer.beginning_offsets([partition])
                    if beginnings[partition] != 0:
                        raise RuntimeError("INITIAL_OFFSET_REQUIRES_REVIEW")
                    self.consumer.seek(partition, 0)
                self.states[partition] = PartitionState(
                    object(), 0 if committed is None else committed
                )
        except Exception as error:
            self.assignment_failed = True
            self.failed = True
            await self.on_partitions_revoked(tuple(self.states))
            print(
                json.dumps(
                    {
                        "event": "fanout.assignment_failed",
                        "reason": "offset_initialization_failed",
                        "error_class": type(error).__name__,
                    }
                ),
                flush=True,
            )
        else:
            if assigned:
                self.consumer.resume(*assigned)
        finally:
            self.assignment_ready.set()

    def owns(self, partition: TopicPartition, token: object | None) -> bool:
        state = self.states.get(partition)
        return (
            not self.stopping
            and token is not None
            and state is not None
            and state.ownership_token is token
        )

    async def process(
        self,
        partition: TopicPartition,
        messages: Sequence[ConsumerRecord[bytes | None, bytes | None]],
        token: object,
    ) -> None:
        for message in messages:
            try:
                if message.value is None or message.key is None:
                    raise ValueError("MISSING_EVENT_DATA")
                event = decode_event(message.value, message.key)
            except ValueError, TypeError:
                self.failed = True
                print(
                    json.dumps(
                        {
                            "event": "fanout.paused",
                            "reason": "unsupported_event",
                            "partition": partition.partition,
                        }
                    ),
                    flush=True,
                )
                return
            while self.owns(partition, token):
                try:
                    await self.delivery.deliver(event)
                    if not self.owns(partition, token):
                        return
                    await self.consumer.commit({partition: message.offset + 1})
                except asyncio.CancelledError:
                    raise
                except Exception:
                    print(
                        json.dumps(
                            {"event": "fanout.retry", "partition": partition.partition}
                        ),
                        flush=True,
                    )
                    await asyncio.sleep(0.5)
                else:
                    break
            if not self.owns(partition, token):
                return
        if self.owns(partition, token):
            self.consumer.resume(partition)

    def dispatch(
        self,
        partition: TopicPartition,
        messages: Sequence[ConsumerRecord[bytes | None, bytes | None]],
    ) -> None:
        state = self.states.get(partition)
        if state is None or not messages:
            return
        previous = state.task
        if previous is not None and not previous.done():
            raise RuntimeError("PARTITION_QUEUE_INVARIANT")
        if (
            previous is not None
            and not previous.cancelled()
            and previous.exception() is not None
        ):
            raise RuntimeError("PARTITION_WORKER_FAILED")
        self.consumer.pause(partition)
        token = state.ownership_token
        if token is None:
            return
        state.task = asyncio.create_task(self.process(partition, messages, token))

    def check_workers(self) -> None:
        for state in self.states.values():
            task = state.task
            if (
                task is not None
                and task.done()
                and not task.cancelled()
                and task.exception() is not None
            ):
                raise RuntimeError("PARTITION_WORKER_FAILED")

    async def rewind(self) -> None:
        await self.assignment_ready.wait()
        if self.assignment_failed:
            raise RuntimeError("ASSIGNMENT_INITIALIZATION_FAILED")
        for partition, state in self.states.items():
            self.consumer.seek(partition, state.assignment_start_offset)

    async def run(self) -> None:
        while not self.stopping:
            # SDK listener seek가 끝나기 전에 start()가 반환할 수 있습니다.
            await self.assignment_ready.wait()
            if self.assignment_failed:
                raise RuntimeError("ASSIGNMENT_INITIALIZATION_FAILED")
            generation = self.assignment_generation
            try:
                batches = await self.consumer.getmany(timeout_ms=200, max_records=16)
            except NoOffsetForPartitionError:
                if generation is not self.assignment_generation:
                    # poll이 새 assignment cursor를 전진시켰을 수 있어 시작 offset으로 되감습니다.
                    await self.rewind()
                    continue
                raise
            if generation is not self.assignment_generation:
                # 결과만 버리면 다음 commit이 새 assignment record를 건너뜁니다.
                await self.rewind()
                continue
            for partition, messages in batches.items():
                self.dispatch(partition, messages)
            self.check_workers()

    async def close(self) -> None:
        self.stopping = True
        await self.on_partitions_revoked(tuple(self.states))
