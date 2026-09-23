"""Redis TIME와 Lua로 주소·방 구독을 원자적으로 갱신합니다. Pub/Sub은 없습니다."""

from uuid import UUID

from redis.asyncio import Redis

from chat_service.contracts.subscriptions import GatewayTarget, RegistryUnavailable
from chat_service.core.realtime_settings import validate_ip

PREFIX = "chat:registry:"
MAX_ROOMS = 256
MAX_TARGETS = 16

SYNC = """
local now = redis.call('TIME'); local expiry = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2])/1000) + 30000
local old = redis.call('SMEMBERS', KEYS[2]); local wanted = {}
for i=4,#ARGV do wanted[ARGV[i]]=true end
for _,room in ipairs(old) do
  if not wanted[room] then redis.call('ZREM', ARGV[1] .. 'room:' .. room, ARGV[2]) end
end
redis.call('DEL', KEYS[2])
for i=4,#ARGV do
  local key = ARGV[1] .. 'room:' .. ARGV[i]
  redis.call('ZREMRANGEBYSCORE', key, '-inf', expiry-30000)
  redis.call('ZADD', key, expiry, ARGV[2]); redis.call('PEXPIRE', key, 30000)
  redis.call('SADD', KEYS[2], ARGV[i])
end
redis.call('PEXPIRE', KEYS[2], 30000)
redis.call('SET', KEYS[1], ARGV[3], 'PX', 30000)
return 1
"""
REMOVE = """
for _,room in ipairs(redis.call('SMEMBERS', KEYS[2])) do redis.call('ZREM', ARGV[1] .. 'room:' .. room, ARGV[2]) end
redis.call('DEL', KEYS[1], KEYS[2]); return 1
"""
TARGETS = """
local now=redis.call('TIME'); local ms=tonumber(now[1])*1000+math.floor(tonumber(now[2])/1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ms)
if redis.call('ZCARD', KEYS[1]) > 16 then return redis.error_reply('TARGET_LIMIT') end
local members=redis.call('ZRANGE', KEYS[1], 0, 16); local result={}
for _,member in ipairs(members) do
  local address=redis.call('GET', ARGV[1] .. 'instance:' .. member)
  if address then table.insert(result,member);table.insert(result,address)
  else redis.call('ZREM',KEYS[1],member) end
end
return result
"""


class RedisRegistry:
    def __init__(self, redis: Redis, *, allow_loopback: bool = False) -> None:
        self.redis = redis
        self.allow_loopback = allow_loopback

    async def sync(self, target: GatewayTarget, rooms: set[UUID]) -> None:
        if len(rooms) > MAX_ROOMS:
            raise RegistryUnavailable("ROOM_LIMIT")
        validate_ip(target.ip, self.allow_loopback)
        # 이 인스턴스의 호출은 GatewayHub의 한 lock 안에서만 수행합니다.
        await self.redis.eval(
            SYNC,
            2,
            PREFIX + "instance:" + str(target.instance_id),
            PREFIX + "owned:" + str(target.instance_id),
            PREFIX,
            str(target.instance_id),
            target.ip,
            *(str(room) for room in sorted(rooms)),
        )

    async def remove(self, target: GatewayTarget) -> None:
        await self.redis.eval(
            REMOVE,
            2,
            PREFIX + "instance:" + str(target.instance_id),
            PREFIX + "owned:" + str(target.instance_id),
            PREFIX,
            str(target.instance_id),
        )

    async def targets(self, room: UUID) -> list[GatewayTarget]:
        raw = await self.redis.eval(TARGETS, 1, PREFIX + "room:" + str(room), PREFIX)
        if len(raw) % 2 or len(raw) > MAX_TARGETS * 2:
            raise RegistryUnavailable("TARGET_LIMIT_OR_INVALID_DATA")
        result = []
        for index in range(0, len(raw), 2):
            identity = UUID(raw[index])
            address = validate_ip(raw[index + 1], self.allow_loopback)
            result.append(GatewayTarget(identity, address))
        return result
