import logging
from datetime import datetime, timezone

from app.config import get_settings
from app.infra.redis_client import get_redis

log = logging.getLogger("policydesk.spend")
PAID_BACKENDS = {"bedrock", "bedrock_agent"}


def _day() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _paid(backend: str) -> bool:
    return backend in PAID_BACKENDS


async def add_cost(backend: str, usd: float) -> None:
    if not _paid(backend) or usd <= 0:
        return
    key = f"spend:aws:{_day()}"
    pipe = get_redis().pipeline()
    pipe.incrbyfloat(key, usd)
    pipe.expire(key, 172800)
    total, _ = await pipe.execute()
    limit = get_settings().aws_daily_usd_limit
    if float(total) >= limit:
        log.error("AWS daily spend cap reached: $%.4f of $%.2f; Bedrock is paused until tomorrow (UTC)", float(total), limit)


async def spent_today() -> float:
    v = await get_redis().get(f"spend:aws:{_day()}")
    return float(v) if v else 0.0


async def record_failure(backend: str) -> None:
    if not _paid(backend):
        return
    s = get_settings()
    r = get_redis()
    key = f"circuit:{backend}:failures"
    n = await r.incr(key)
    if n == 1:
        await r.expire(key, s.circuit_window_s)
    if n >= s.circuit_max_failures:
        await r.set(f"circuit:{backend}:open", "1", ex=s.circuit_cooldown_s)
        await r.delete(key)
        log.error("%s failed %d times in %ds; pausing it for %ds", backend, n, s.circuit_window_s, s.circuit_cooldown_s)


async def record_success(backend: str) -> None:
    if _paid(backend):
        await get_redis().delete(f"circuit:{backend}:failures")


async def blocked_reason(backend: str) -> str | None:
    if not _paid(backend):
        return None
    s = get_settings()
    if not s.aws_enabled:
        return "aws_disabled"
    if await get_redis().exists(f"circuit:{backend}:open"):
        return "circuit_open"
    if await spent_today() >= s.aws_daily_usd_limit:
        return "spend_cap"
    return None


async def status() -> dict:
    s = get_settings()
    r = get_redis()
    return {
        "aws_enabled": s.aws_enabled,
        "spent_today_usd": round(await spent_today(), 6),
        "daily_limit_usd": s.aws_daily_usd_limit,
        "bedrock_paused": bool(await r.exists("circuit:bedrock:open")),
        "agent_paused": bool(await r.exists("circuit:bedrock_agent:open")),
    }
