import logging
import uuid
from datetime import datetime, timezone

from app.db.pool import get_pool
from app.infra import cache, spend
from app.infra.broker import USAGE, broker
from app.providers.pricing import cost_usd

log = logging.getLogger("policydesk.usage")


def usage_event(tenant_id: str, user_id: str | None, kind: str, backend: str, model: str, input_tokens: int, output_tokens: int, latency_ms: int | None, cached: bool = False) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "user_id": user_id,
        "kind": kind,
        "backend": backend,
        "model": model,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "cost_usd": cost_usd(backend, model, input_tokens, output_tokens),
        "latency_ms": latency_ms,
        "cached": cached,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


async def insert_usage(ev: dict, conn=None) -> bool:
    sql = """
        INSERT INTO usage_ledger (event_id, tenant_id, user_id, kind, backend, model, input_tokens, output_tokens, cost_usd, latency_ms, cached, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        ON CONFLICT DO NOTHING
    """
    args = (
        uuid.UUID(ev["event_id"]),
        uuid.UUID(ev["tenant_id"]),
        uuid.UUID(ev["user_id"]) if ev.get("user_id") else None,
        ev["kind"],
        ev["backend"],
        ev["model"],
        ev["input_tokens"],
        ev["output_tokens"],
        ev["cost_usd"],
        ev.get("latency_ms"),
        ev.get("cached", False),
        datetime.fromisoformat(ev["created_at"]),
    )
    if conn is not None:
        res = await conn.execute(sql, *args)
    else:
        res = await get_pool().execute(sql, *args)
    return res.endswith("1")


async def record(ev: dict) -> None:
    if not ev["cached"]:
        await cache.add_tokens(ev["tenant_id"], ev["input_tokens"] + ev["output_tokens"])
        await spend.add_cost(ev["backend"], ev["cost_usd"])
    try:
        await broker.publish(USAGE, ev, message_id=ev["event_id"])
    except Exception as e:
        log.warning("usage queue unavailable (%s); writing directly", e)
        await insert_usage(ev)
