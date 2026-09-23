import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiokafka import ConsumerRecord, TopicPartition
from aiokafka.errors import NoOffsetForPartitionError

from chat_service.contracts.subscriptions import GatewayTarget
from chat_service.schemas.chat import MessageCreated, MessageData
from chat_service.services.fanout import (
    FanoutDelivery,
    PartitionFanout,
    PartitionState,
    decode_event,
)


def event():
    identity = uuid4()
    return MessageCreated(
        event_id=identity,
        message=MessageData(
            message_id=identity,
            conversation_id=uuid4(),
            sender_id=uuid4(),
            client_message_id=uuid4(),
            seq="1",
            text="synthetic",
            created_at="2026-09-08T00:00:00Z",
        ),
    )


def consumer_record(
    value: bytes | None, key: bytes | None, offset: int
) -> ConsumerRecord[bytes | None, bytes | None]:
    return ConsumerRecord(
        "chat.message-created.v1",
        0,
        offset,
        0,
        0,
        key,
        value,
        None,
        0 if key is None else len(key),
        0 if value is None else len(value),
        (),
    )


class Consumer:
    def __init__(self):
        self.commits = []
        self.resumed = []

    async def commit(self, offsets):
        self.commits.append(offsets)

    def resume(self, *partitions):
        self.resumed.extend(partitions)

    def pause(self, *partitions):
        pass

    async def committed(self, partition, /):
        return 0

    async def beginning_offsets(self, partitions, /):
        return {partition: 0 for partition in partitions}

    def seek(self, partition, offset, /):
        pass

    async def getmany(self, **kwargs):
        return {}


def test_decode_requires_explicit_schema_key_and_id():
    message = event()
    assert (
        decode_event(
            message.model_dump_json().encode(),
            str(message.message.conversation_id).encode(),
        )
        == message
    )
    with pytest.raises(ValueError):
        decode_event(b'{"type":"message.created","schema_version":2}', b"x")
    with pytest.raises(ValueError):
        decode_event(message.model_dump_json().encode(), b"other-room")


def test_fanout_requires_all_current_targets_and_handles_ttl_expiry():
    first, second = (
        GatewayTarget(uuid4(), "10.42.0.10"),
        GatewayTarget(uuid4(), "10.42.0.11"),
    )

    class Registry:
        current = [first, second]

        async def sync(self, target, rooms):
            raise AssertionError("LOOKUP_ONLY")

        async def remove(self, target):
            raise AssertionError("LOOKUP_ONLY")

        async def targets(self, room):
            return self.current

    async def scenario():
        registry = Registry()

        async def send(target, event):
            if target == second:
                raise TimeoutError

        with pytest.raises(RuntimeError):
            await FanoutDelivery(registry, send).deliver(event())

        async def expired(target, event):
            if target == second:
                registry.current = [first]
                raise TimeoutError

        await FanoutDelivery(registry, expired).deliver(event())

    asyncio.run(scenario())


def test_commit_after_acceptance_and_revoke_fences_worker():
    async def scenario():
        consumer = Consumer()
        accepted = asyncio.Event()

        class Delivery(FanoutDelivery):
            def __init__(self):
                pass

            async def deliver(self, event):
                await accepted.wait()

        fanout = PartitionFanout(consumer, Delivery())
        partition = TopicPartition("chat.message-created.v1", 0)
        epoch = object()
        fanout.states[partition] = PartitionState(epoch, 0)
        message = event()
        record = consumer_record(
            message.model_dump_json().encode(),
            str(message.message.conversation_id).encode(),
            7,
        )
        task = asyncio.create_task(fanout.process(partition, [record], epoch))
        await asyncio.sleep(0)
        assert consumer.commits == []
        accepted.set()
        await task
        assert consumer.commits == [{partition: 8}]
        accepted.clear()
        task = asyncio.create_task(fanout.process(partition, [record], epoch))
        fanout.states[partition].task = task
        await asyncio.sleep(0)
        await fanout.on_partitions_revoked([partition])
        accepted.set()
        assert task.cancelled() and consumer.commits == [{partition: 8}]

    asyncio.run(scenario())


def test_unknown_schema_never_commits_or_resumes_partition():
    async def scenario():
        consumer = Consumer()
        fanout = PartitionFanout(consumer, AsyncMock(spec=FanoutDelivery))
        partition, epoch = TopicPartition("chat.message-created.v1", 0), object()
        fanout.states[partition] = PartitionState(epoch, 0)
        await fanout.process(partition, [consumer_record(b"{}", b"x", 4)], epoch)
        assert fanout.failed and not consumer.commits and not consumer.resumed

    asyncio.run(scenario())


def test_start_waits_until_initial_seek_completed():
    async def scenario():
        release = asyncio.Event()
        partition = TopicPartition("chat.message-created.v1", 0)

        class DelayedConsumer(Consumer):
            polls = 0
            sought = []

            def pause(self, *partitions):
                pass

            async def committed(self, tp):
                await release.wait()
                return None

            async def beginning_offsets(self, partitions):
                return {partition: 0}

            def seek(self, tp, offset):
                self.sought.append((tp, offset))

            async def getmany(self, **kwargs):
                self.polls += 1
                assert self.sought == [(partition, 0)]
                raise RuntimeError("STOP_TEST")

        consumer = DelayedConsumer()
        fanout = PartitionFanout(consumer, AsyncMock(spec=FanoutDelivery))
        runner = asyncio.create_task(fanout.run())
        assignment = asyncio.create_task(fanout.on_partitions_assigned({partition}))
        await asyncio.sleep(0)
        assert consumer.polls == 0
        release.set()
        await assignment
        with pytest.raises(RuntimeError, match="STOP_TEST"):
            await runner
        assert consumer.polls == 1

    asyncio.run(scenario())


def test_rebalance_during_poll_rewinds_new_cursor_before_next_commit():
    async def scenario():
        partition = TopicPartition("chat.message-created.v1", 0)
        message = event()

        def record(offset):
            return consumer_record(
                message.model_dump_json().encode(),
                str(message.message.conversation_id).encode(),
                offset,
            )

        class RebalancingConsumer(Consumer):
            polls = 0
            sought = []

            def pause(self, *partitions):
                pass

            async def committed(self, tp):
                return 3

            def seek(self, tp, offset):
                self.sought.append((tp, offset))

            async def getmany(self, **kwargs):
                self.polls += 1
                if self.polls == 1:
                    await fanout.on_partitions_revoked({partition})
                    await fanout.on_partitions_assigned({partition})
                    return {partition: [record(77)]}
                if self.polls == 2:
                    assert self.sought == [(partition, 3)]
                    return {partition: [record(3)]}
                await asyncio.sleep(0.01)
                fanout.stopping = True
                return {}

        consumer = RebalancingConsumer()
        fanout = PartitionFanout(consumer, AsyncMock(spec=FanoutDelivery))
        await fanout.on_partitions_assigned({partition})
        await fanout.run()
        assert consumer.commits == [{partition: 4}]
        assert consumer.sought == [(partition, 3)]
        await fanout.close()

    asyncio.run(scenario())


def test_assignment_failure_and_stable_missing_offset_fail_closed(capsys):
    async def scenario():
        partition = TopicPartition("chat.message-created.v1", 0)

        class BrokenConsumer(Consumer):
            def pause(self, *partitions):
                pass

            async def committed(self, tp):
                raise TimeoutError("DO_NOT_LOG_SECRET")

            async def getmany(self, **kwargs):
                raise NoOffsetForPartitionError(partition)

        consumer = BrokenConsumer()
        fanout = PartitionFanout(consumer, AsyncMock(spec=FanoutDelivery))
        await fanout.on_partitions_assigned({partition})
        assert fanout.failed and not fanout.states
        with pytest.raises(RuntimeError, match="INITIALIZATION_FAILED"):
            await fanout.run()
        assert not consumer.commits
        assert "DO_NOT_LOG_SECRET" not in capsys.readouterr().out
        fanout.assignment_failed = False
        with pytest.raises(NoOffsetForPartitionError):
            await fanout.run()
        assert not consumer.commits

    asyncio.run(scenario())
