import asyncio
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from chat_service.adapters.kafka_publisher import KafkaPublisher
from chat_service.contracts.relay import RelayClaim
from chat_service.core.relay_settings import RelaySettings


def test_lab_settings_fail_closed():
    valid: dict[str, Any] = dict(
        app_environment="isolated-lab",
        network_profile="isolated-lab",
        db_primary_url="postgresql+asyncpg://chat_writer:synthetic@127.0.0.1:5440/laughtale_chat",
        kafka_bootstrap_servers="127.0.0.1:19092",
    )
    assert RelaySettings(_env_file=None, **valid)
    for changes in (
        {"app_environment": "production"},
        {"network_profile": "local"},
        {"kafka_bootstrap_servers": "example.org:9092"},
        {
            "db_primary_url": "postgresql+asyncpg://chat_writer:synthetic@127.0.0.1:5433/laughtale_chat"
        },
    ):
        with pytest.raises(ValidationError):
            invalid: dict[str, Any] = valid | changes
            RelaySettings(_env_file=None, **invalid)


def test_publisher_ack_wire_and_retry_recreates_producer(monkeypatch):
    instances = []

    class Producer:
        def __init__(self, **options):
            self.options = options
            self.stopped = False
            self.sent = None
            instances.append(self)

        async def start(self):
            pass

        async def send_and_wait(self, topic, **wire):
            self.sent = (topic, wire)
            if len(instances) == 1:
                raise TimeoutError()

        async def stop(self):
            self.stopped = True

    monkeypatch.setattr(
        "chat_service.adapters.kafka_publisher.AIOKafkaProducer", Producer
    )

    async def scenario():
        publisher = KafkaPublisher("kafka:9092")
        claim = RelayClaim(uuid4(), uuid4(), uuid4(), 1, {"test": "한글"})
        with pytest.raises(TimeoutError):
            await publisher.publish(claim)
        assert instances[0].stopped
        await publisher.publish(claim)
        assert len(instances) == 2
        assert instances[1].options["enable_idempotence"]
        assert instances[1].options["acks"] == "all"
        assert instances[1].sent == (
            "chat.message-created.v1",
            {
                "key": str(claim.conversation_id).encode("ascii"),
                "value": '{"test":"한글"}'.encode(),
            },
        )
        await publisher.close()

    asyncio.run(scenario())
