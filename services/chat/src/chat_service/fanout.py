"""python -m chat_service.fanout: 고정 consumer group의 독립 실행입니다."""

import asyncio
import json
import logging
import signal

import httpx2
from aiokafka import AIOKafkaConsumer
from redis.asyncio import Redis

from chat_service.adapters.gateway_delivery import GatewayDeliveryClient
from chat_service.adapters.redis_registry import RedisRegistry
from chat_service.core.realtime_settings import RealtimeSettings
from chat_service.services.fanout import GROUP, TOPIC, FanoutDelivery, PartitionFanout


async def run(settings: RealtimeSettings) -> None:
    consumer = AIOKafkaConsumer(
        **(
            {"client_id": f"chat-fanout-{settings.lab_pod_uid}"}
            if settings.lab_pod_uid
            else {}
        ),
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id=GROUP,
        enable_auto_commit=False,
        auto_offset_reset="none",
        max_poll_records=16,
        max_partition_fetch_bytes=262144,
        fetch_max_bytes=1048576,
        max_poll_interval_ms=30000,
        session_timeout_ms=10000,
        heartbeat_interval_ms=3000,
        request_timeout_ms=10000,
    )
    redis = Redis.from_url(
        settings.redis_url.get_secret_value(),
        decode_responses=True,
        socket_timeout=1,
        socket_connect_timeout=1,
        max_connections=4,
    )
    async with httpx2.AsyncClient(
        timeout=1,
        follow_redirects=False,
        trust_env=False,
        limits=httpx2.Limits(max_connections=4, max_keepalive_connections=4),
    ) as client:
        gateway = GatewayDeliveryClient(
            client,
            settings.gateway_delivery_token.get_secret_value(),
            allow_loopback=settings.realtime_allow_loopback,
        )
        registry = RedisRegistry(redis, allow_loopback=settings.realtime_allow_loopback)
        fanout = PartitionFanout(consumer, FanoutDelivery(registry, gateway.send))
        consumer.subscribe([TOPIC], listener=fanout)
        task = asyncio.current_task()
        assert task is not None
        loop = asyncio.get_running_loop()
        for name in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(name, task.cancel)
        try:
            await consumer.start()
            if consumer.partitions_for_topic(TOPIC) != {0, 1, 2, 3}:
                raise RuntimeError("FIXED_FOUR_PARTITIONS_REQUIRED")
            await fanout.run()
        except asyncio.CancelledError:
            pass
        finally:
            await fanout.close()
            async with asyncio.timeout(5):
                await consumer.stop()
                await redis.aclose()


def main() -> None:
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(run(RealtimeSettings()))
    except Exception as error:
        print(
            json.dumps(
                {
                    "event": "fanout.failed",
                    "reason": "startup_or_poll_failed",
                    "error_class": type(error).__name__,
                }
            ),
            flush=True,
        )
        raise SystemExit(
            "Fanout failed; offsets were not advanced for unconfirmed work"
        ) from None


if __name__ == "__main__":
    main()
