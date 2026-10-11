import uuid

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import get_settings
from app.db.pool import get_pool
from app.infra import cache, spend
from app.infra.broker import broker
from app.security import CurrentUser, admin_user
from app.services import ingest

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/usage")
async def usage_summary(days: int = Query(default=14, ge=1, le=90), user: CurrentUser = Depends(admin_user)):
    s = get_settings()
    pool = get_pool()
    tid = uuid.UUID(user.tenant_id)
    daily = await pool.fetch(
        """
        SELECT date_trunc('day', created_at)::date AS day,
               sum(input_tokens + output_tokens) AS tokens,
               sum(cost_usd) AS cost_usd,
               count(*) FILTER (WHERE kind = 'chat') AS chats,
               count(*) FILTER (WHERE kind = 'agent') AS agent_steps,
               count(*) FILTER (WHERE kind = 'chat' AND cached) AS cached_chats
        FROM usage_ledger
        WHERE tenant_id = $1 AND created_at >= date_trunc('day', now()) - make_interval(days => $2 - 1)
        GROUP BY 1 ORDER BY 1
        """,
        tid, days,
    )
    by_model = await pool.fetch(
        """
        SELECT backend, model, kind, count(*) AS calls, sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens, sum(cost_usd) AS cost_usd
        FROM usage_ledger
        WHERE tenant_id = $1 AND created_at >= now() - make_interval(days => $2)
        GROUP BY 1, 2, 3 ORDER BY cost_usd DESC, calls DESC
        """,
        tid, days,
    )
    latency = await pool.fetchrow(
        """
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95,
               count(*) FILTER (WHERE cached)::float / greatest(count(*), 1) AS cache_hit_rate
        FROM usage_ledger
        WHERE tenant_id = $1 AND kind = 'chat' AND created_at >= now() - make_interval(days => $2)
        """,
        tid, days,
    )
    docs = await pool.fetch("SELECT status, count(*) AS n FROM documents WHERE tenant_id = $1 GROUP BY status", tid)
    try:
        queues = await broker.queue_depths()
    except Exception:
        queues = {}
    return {
        "days": days,
        "daily": [dict(r) for r in daily],
        "by_model": [dict(r) for r in by_model],
        "latency_ms": {"p50": latency["p50"], "p95": latency["p95"]},
        "cache_hit_rate": round(latency["cache_hit_rate"] or 0, 4),
        "budget": {"used_today": await cache.tokens_used_today(user.tenant_id), "daily_limit": s.daily_token_budget, "fallback": s.fallback_backend, "global_used_today": await cache.global_tokens_today(), "global_daily_limit": s.global_daily_token_budget},
        "documents": {r["status"]: r["n"] for r in docs},
        "queues": queues,
        "aws": await spend.status(),
        "backend": {"ai": s.ai_backend, "embeddings": s.embed_backend, "retriever": s.retriever, "agent": s.agent_mode},
    }


@router.post("/kb/sync", status_code=202)
async def kb_sync(user: CurrentUser = Depends(admin_user)):
    try:
        n = await ingest.push_tenant_docs_to_kb(user.tenant_id)
    except ingest.UploadError as e:
        raise HTTPException(e.status, str(e))
    return {"uploaded": n, "status": "sync queued"}
