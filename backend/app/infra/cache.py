import hashlib
import json
import re
from datetime import datetime, timezone

from app.config import get_settings
from app.infra.redis_client import get_redis

_ws = re.compile(r"\s+")
_punct = re.compile(r"[^\w\s]")


def normalize_question(q: str) -> str:
    return _ws.sub(" ", _punct.sub(" ", q.lower())).strip()


async def doc_version(tenant_id: str) -> int:
    v = await get_redis().get(f"docver:{tenant_id}")
    return int(v) if v else 0


async def bump_doc_version(tenant_id: str) -> int:
    return int(await get_redis().incr(f"docver:{tenant_id}"))


async def answer_key(tenant_id: str, question: str, backend: str) -> str:
    v = await doc_version(tenant_id)
    digest = hashlib.sha256(f"{backend}|{normalize_question(question)}".encode()).hexdigest()[:32]
    return f"ans:{tenant_id}:v{v}:{digest}"


async def get_answer(key: str) -> dict | None:
    raw = await get_redis().get(key)
    if raw:
        await get_redis().incr("stats:cache:hit")
        return json.loads(raw)
    await get_redis().incr("stats:cache:miss")
    return None


async def set_answer(key: str, payload: dict) -> None:
    await get_redis().set(key, json.dumps(payload), ex=get_settings().cache_ttl_s)


def _day() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


async def tokens_used_today(tenant_id: str) -> int:
    v = await get_redis().get(f"budget:{tenant_id}:{_day()}")
    return int(v) if v else 0


async def add_tokens(tenant_id: str, tokens: int) -> int:
    key = f"budget:{tenant_id}:{_day()}"
    gkey = f"budget:global:{_day()}"
    pipe = get_redis().pipeline()
    pipe.incrby(key, tokens)
    pipe.expire(key, 172800)
    pipe.incrby(gkey, tokens)
    pipe.expire(gkey, 172800)
    total, _, _, _ = await pipe.execute()
    return int(total)


async def budget_exhausted(tenant_id: str, limit: int | None = None) -> bool:
    cap = limit or get_settings().daily_token_budget
    return await tokens_used_today(tenant_id) >= cap


async def global_tokens_today() -> int:
    v = await get_redis().get(f"budget:global:{_day()}")
    return int(v) if v else 0


async def global_budget_exhausted() -> bool:
    return await global_tokens_today() >= get_settings().global_daily_token_budget


async def cache_stats() -> dict:
    r = get_redis()
    hit, miss = await r.mget("stats:cache:hit", "stats:cache:miss")
    hit, miss = int(hit or 0), int(miss or 0)
    total = hit + miss
    return {"hits": hit, "misses": miss, "hit_rate": round(hit / total, 4) if total else 0.0}
