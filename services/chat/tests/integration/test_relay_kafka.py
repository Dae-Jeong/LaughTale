"""Explicitly enabled, six-event real Kafka/isolated PostgreSQL smoke test."""

import asyncio
import json
import os
from uuid import uuid4

import pytest
from aiokafka import AIOKafkaConsumer, TopicPartition
from sqlalchemy import select
from tests.integration.test_chat_service import A, B, service_database

from chat_service.adapters.kafka_publisher import TOPIC, KafkaPublisher
from chat_service.domain.chat import MessagePayload
from chat_service.models.chat import Conversation, Member, MessageOutbox
from chat_service.services.relay import OutboxRelay

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.environ.get("CHAT_TEST_KAFKA") != "1",
        reason="Explicit Kafka lab opt-in required",
    ),
]


def test_two_relays_publish_six_messages_in_room_order(postgres_url):
    async def scenario():
        async with service_database(postgres_url) as service:
            room = uuid4()
            async with service.factory() as session, session.begin():
                session.add(Conversation(id=room, kind="dm"))
                await session.flush()
                session.add_all(
                    [Member(conversation_id=room, user_id=u) for u in (A, B)]
                )
            expected = []
            for i in range(6):
                stored = await service.store(
                    A, room, uuid4(), MessagePayload(f"relay-smoke-{i}")
                )
                expected.append(str(stored.message.message_id))
            consumer = AIOKafkaConsumer(
                TOPIC,
                bootstrap_servers="127.0.0.1:19092",
                group_id=None,
                enable_auto_commit=False,
            )
            publishers = [KafkaPublisher("127.0.0.1:19092") for _ in range(2)]
            received = []
            try:
                await consumer.start()
                await consumer.topics()
                partitions = consumer.partitions_for_topic(TOPIC)
                assert partitions
                consumer.unsubscribe()
                consumer.assign([TopicPartition(TOPIC, p) for p in partitions])
                await consumer.seek_to_end()
                relays = [OutboxRelay(service.factory, p) for p in publishers]
                async with asyncio.timeout(25):
                    for _ in range(6):
                        outcomes = await asyncio.gather(*(r.once() for r in relays))
                        assert all(o in {"published", "idle"} for o in outcomes)
                    while len(received) < 6:
                        batches = await consumer.getmany(
                            timeout_ms=1000, max_records=100
                        )
                        for records in batches.values():
                            for record in records:
                                if record.key == str(room).encode():
                                    assert isinstance(record.value, bytes), (
                                        "Expected a non-null Kafka event payload"
                                    )
                                    received.append(json.loads(record.value))
                assert [e["message"]["message_id"] for e in received] == expected
                assert [int(e["message"]["seq"]) for e in received] == list(range(1, 7))
                async with service.factory() as session:
                    rows = (await session.scalars(select(MessageOutbox))).all()
                    assert len(rows) == 6 and all(
                        r.published_at and r.claim_token is None for r in rows
                    )
            finally:
                await asyncio.gather(*(p.close() for p in publishers))
                await consumer.stop()

    asyncio.run(scenario())
