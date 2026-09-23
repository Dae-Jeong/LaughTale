"""관리자가 지정한 전용 Redis에서만 실행하며 FLUSH/광역 삭제를 하지 않습니다."""

import asyncio
import os
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from chat_service.adapters.redis_registry import PREFIX, RedisRegistry
from chat_service.contracts.subscriptions import GatewayTarget


@pytest.mark.skipif(
    not os.environ.get("REDIS_REGISTRY_TEST_URL"),
    reason="explicit dedicated Redis required",
)
def test_real_lua_registration_ttl_stale_instance_and_cleanup():
    url = os.environ["REDIS_REGISTRY_TEST_URL"]
    parsed = urlsplit(url)
    assert (parsed.hostname, parsed.port, parsed.path) == ("127.0.0.1", 19079, "/0")

    async def scenario():
        redis = Redis.from_url(
            url, decode_responses=True, socket_timeout=1, socket_connect_timeout=1
        )
        registry = RedisRegistry(redis)
        target = GatewayTarget(uuid4(), "10.42.0.10")
        other = GatewayTarget(uuid4(), "10.42.0.11")
        first, second = uuid4(), uuid4()
        try:
            await registry.sync(target, {first})
            assert await registry.targets(first) == [target]
            ttl = await redis.pttl(PREFIX + "instance:" + str(target.instance_id))
            assert 0 < ttl <= 30000
            await registry.sync(other, {first})
            await registry.sync(target, {second})
            assert await registry.targets(first) == [other]
            assert await registry.targets(second) == [target]
            await redis.zadd(PREFIX + "room:" + str(first), {str(other.instance_id): 0})
            assert await registry.targets(first) == []
            await registry.sync(target, {first})
            assert await registry.targets(second) == []
            await registry.remove(target)
            assert await registry.targets(first) == []
        finally:
            await registry.remove(target)
            await registry.remove(other)
            await redis.delete(
                PREFIX + "room:" + str(first), PREFIX + "room:" + str(second)
            )
            await redis.aclose()

    asyncio.run(scenario())
