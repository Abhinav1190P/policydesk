import time
from dataclasses import dataclass

from app.infra.redis_client import get_redis

TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_ms = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local state = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil then
  tokens = capacity
  ts = now
end
local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + elapsed * refill_per_ms)
local allowed = 0
local retry_ms = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  retry_ms = math.ceil((cost - tokens) / refill_per_ms)
end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', key, math.ceil(capacity / refill_per_ms) + 1000)
return {allowed, math.floor(tokens), retry_ms}
"""

_script = None


@dataclass
class RateResult:
    allowed: bool
    remaining: int
    retry_after_ms: int


async def take(key: str, per_minute: int, burst: int, cost: int = 1) -> RateResult:
    global _script
    r = get_redis()
    if _script is None:
        _script = r.register_script(TOKEN_BUCKET_LUA)
    capacity = max(burst, 1)
    refill_per_ms = per_minute / 60000.0
    allowed, remaining, retry = await _script(
        keys=[f"rl:{key}"], args=[capacity, refill_per_ms, int(time.time() * 1000), cost]
    )
    return RateResult(bool(allowed), int(remaining), int(retry))


def reset_script_cache() -> None:
    global _script
    _script = None
