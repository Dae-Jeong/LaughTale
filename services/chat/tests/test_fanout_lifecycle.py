import asyncio
from unittest.mock import AsyncMock

import pytest
from aiokafka import TopicPartition

from chat_service.services.fanout import FanoutDelivery, PartitionFanout, PartitionState
from tests.test_fanout import Consumer, consumer_record, event


class AssigningConsumer(Consumer):
    def __init__(self) -> None:
        super().__init__()
        self.broken: TopicPartition | None = None

    def pause(self, *partitions) -> None:
        pass

    async def committed(self, partition):
        if partition == self.broken:
            raise TimeoutError("synthetic offset failure")
        return 3


def test_assignment_failure_cancels_and_awaits_existing_worker() -> None:
    async def scenario() -> None:
        consumer = AssigningConsumer()
        fanout = PartitionFanout(consumer, AsyncMock(spec=FanoutDelivery))
        old, new = TopicPartition("chat", 0), TopicPartition("chat", 1)
        await fanout.on_partitions_assigned([old])
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def worker() -> None:
            entered.set()
            try:
                await asyncio.Future()
            finally:
                assert not fanout.owns(old, token)
                cleaned.set()

        token = fanout.states[old].ownership_token
        assert token is not None
        task = asyncio.create_task(worker())
        fanout.states[old].task = task
        await entered.wait()
        consumer.broken = new
        try:
            await fanout.on_partitions_assigned([new])
            assert task.done() and cleaned.is_set()
            assert fanout.assignment_failed and fanout.assignment_ready.is_set()
            assert not consumer.commits
            with pytest.raises(RuntimeError, match="ASSIGNMENT_INITIALIZATION_FAILED"):
                await fanout.run()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_partial_assignment_failure_removes_start_offsets() -> None:
    async def scenario() -> None:
        consumer = AssigningConsumer()
        fanout = PartitionFanout(consumer, AsyncMock(spec=FanoutDelivery))
        first, second = TopicPartition("chat", 0), TopicPartition("chat", 1)
        consumer.broken = second
        await fanout.on_partitions_assigned([first, second])
        assert fanout.assignment_failed
        assert not fanout.states
        assert not consumer.resumed
        await fanout.close()

    asyncio.run(scenario())


def test_close_cleans_task_even_when_ownership_is_already_lost() -> None:
    async def scenario() -> None:
        consumer = AssigningConsumer()
        fanout = PartitionFanout(consumer, AsyncMock(spec=FanoutDelivery))
        partition = TopicPartition("chat", 0)
        await fanout.on_partitions_assigned([partition])

        async def worker() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(worker())
        fanout.states[partition].task = task
        fanout.states[partition].ownership_token = None
        try:
            await fanout.close()
            assert task.done()
            assert not fanout.states
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_assignment_failure_collects_completed_worker_exception() -> None:
    async def scenario() -> None:
        consumer = AssigningConsumer()
        fanout = PartitionFanout(consumer, AsyncMock(spec=FanoutDelivery))
        old, new = TopicPartition("chat", 0), TopicPartition("chat", 1)
        await fanout.on_partitions_assigned([old])

        class ObservedTask(asyncio.Task[None]):
            exception_reads = 0

            def exception(self):
                self.exception_reads += 1
                return super().exception()

        async def fail() -> None:
            raise RuntimeError("synthetic worker failure")

        task = ObservedTask(fail())
        fanout.states[old].task = task
        await asyncio.sleep(0)
        assert task.done()
        consumer.broken = new
        try:
            await fanout.on_partitions_assigned([new])
            assert task.exception_reads > 0
            assert not fanout.states
        finally:
            task.exception()

    asyncio.run(scenario())


def test_revoke_cleanup_preserves_new_assignment() -> None:
    async def scenario() -> None:
        consumer = AssigningConsumer()
        fanout = PartitionFanout(consumer, AsyncMock(spec=FanoutDelivery))
        partition = TopicPartition("chat", 0)
        await fanout.on_partitions_assigned([partition])
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        old_token = fanout.states[partition].ownership_token
        assert old_token is not None

        async def worker() -> None:
            started.set()
            try:
                await asyncio.Future()
            finally:
                assert not fanout.owns(partition, old_token)
                cleaning.set()
                await release.wait()

        task = asyncio.create_task(worker())
        fanout.states[partition].task = task
        await started.wait()
        revoke = asyncio.create_task(fanout.on_partitions_revoked([partition]))
        try:
            await asyncio.wait_for(cleaning.wait(), 1)
            await fanout.on_partitions_assigned([partition])
            new_token = fanout.states[partition].ownership_token
            assert new_token is not None
            release.set()
            await revoke
            assert fanout.owns(partition, new_token)
            assert not fanout.owns(partition, old_token)
        finally:
            release.set()
            await asyncio.gather(revoke, task, return_exceptions=True)
            await fanout.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("same_partition_batch", [True, False])
def test_poll_does_not_hide_completed_worker_failure(
    same_partition_batch: bool,
) -> None:
    async def scenario() -> None:
        partition = TopicPartition("chat", 0)
        message = event()
        record = consumer_record(
            message.model_dump_json().encode(),
            str(message.message.conversation_id).encode(),
            3,
        )

        class PollingConsumer(AssigningConsumer):
            async def getmany(self, **kwargs):
                # Bound the runner even if it incorrectly overwrites the failed task.
                fanout.stopping = True
                return {partition: [record]} if same_partition_batch else {}

        consumer = PollingConsumer()
        delivery = AsyncMock(spec=FanoutDelivery)
        fanout = PartitionFanout(consumer, delivery)
        await fanout.on_partitions_assigned([partition])

        async def fail() -> None:
            raise RuntimeError("synthetic worker failure")

        task = asyncio.create_task(fail())
        fanout.states[partition] = PartitionState(object(), 3, task)
        await asyncio.sleep(0)
        try:
            with pytest.raises(RuntimeError, match="PARTITION_WORKER_FAILED"):
                await fanout.run()
            assert fanout.states[partition].task is task
            delivery.deliver.assert_not_awaited()
        finally:
            task.exception()
            await fanout.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("missing", ["key", "value"])
def test_incomplete_record_is_not_delivered_or_committed(missing: str) -> None:
    async def scenario() -> None:
        consumer = AssigningConsumer()
        delivery = AsyncMock(spec=FanoutDelivery)
        fanout = PartitionFanout(consumer, delivery)
        partition = TopicPartition("chat", 0)
        await fanout.on_partitions_assigned([partition])
        consumer.resumed.clear()
        message = event()
        record = consumer_record(
            None if missing == "value" else message.model_dump_json().encode(),
            None if missing == "key" else str(message.message.conversation_id).encode(),
            3,
        )
        fanout.dispatch(partition, [record])
        task = fanout.states[partition].task
        assert task is not None
        await task
        assert fanout.failed
        assert not consumer.commits and not consumer.resumed
        delivery.deliver.assert_not_awaited()
        await fanout.close()

    asyncio.run(scenario())


def test_dispatch_guards_worker_and_rewind_keeps_assignment_start() -> None:
    async def scenario() -> None:
        sought = []

        class SeekingConsumer(AssigningConsumer):
            def seek(self, partition, offset):
                sought.append((partition, offset))

        consumer = SeekingConsumer()
        release = asyncio.Event()
        delivery = AsyncMock(spec=FanoutDelivery)

        async def accept(message):
            await release.wait()

        delivery.deliver.side_effect = accept
        fanout = PartitionFanout(consumer, delivery)
        partition = TopicPartition("chat", 0)
        await fanout.on_partitions_assigned([partition])
        message = event()
        record = consumer_record(
            message.model_dump_json().encode(),
            str(message.message.conversation_id).encode(),
            3,
        )
        state = fanout.states[partition]
        assert not fanout.owns(partition, None)
        fanout.dispatch(partition, [record])
        task = state.task
        assert task is not None
        try:
            with pytest.raises(RuntimeError, match="PARTITION_QUEUE_INVARIANT"):
                fanout.dispatch(partition, [record])
            assert state.task is task
            release.set()
            await task
            assert consumer.commits == [{partition: 4}]
            assert state.assignment_start_offset == 3
            await fanout.rewind()
            assert sought == [(partition, 3)]
            state.ownership_token = None
            fanout.dispatch(partition, [record])
            assert state.task is task
            assert not fanout.owns(partition, None)
        finally:
            release.set()
            await fanout.close()

    asyncio.run(scenario())
