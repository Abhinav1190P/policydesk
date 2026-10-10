import redis.asyncio as redis

from app.config import get_settings

_redis: redis.Redis | None = None


async def create_redis(url: str | None = None) -> redis.Redis:
    global _redis
    _redis = redis.from_url(url or get_settings().redis_url, decode_responses=True, health_check_interval=30)
    await _redis.ping()
    return _redis


def get_redis() -> redis.Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialised")
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
