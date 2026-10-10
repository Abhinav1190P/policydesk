import argparse
import asyncio
import json
import logging
import os
import re
import statistics
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DATABASE_URL", "postgresql://policydesk:policydesk@localhost:55432/policydesk_eval")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/14")
os.environ["DAILY_TOKEN_BUDGET"] = "100000000"

import asyncpg

from app.agent.runner import LocalAgent
from app.config import get_settings
from app.db.migrate import run_migrations
from app.db.pool import close_pool, create_pool, get_pool
from app.infra.redis_client import close_redis, create_redis, get_redis
from app.providers import factory
from app.rag.prompts import NOT_FOUND
from app.security import CurrentUser
from app.services.chat import run_chat

HERE = Path(__file__).resolve().parent
REFUSAL_MARKERS = ("couldn't find", "could not find", "not in your company", "no information", "don't have information")


def norm(text: str) -> str:
    t = text.lower().replace(",", "")
    t = re.sub(r"[\u2010-\u2015\-]", " ", t).replace("*", "")
    t = re.sub(r"\s+", " ", t)
    return re.sub(r"\s+%", "%", t).strip()


def is_refusal(text: str) -> bool:
    t = text.lower()
    return text.strip() == NOT_FOUND or any(m in t for m in REFUSAL_MARKERS)


async def reset_db() -> None:
    conn = await asyncpg.connect(get_settings().database_url)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    finally:
        await conn.close()


async def users() -> dict:
    rows = await get_pool().fetch(
        "SELECT u.id, u.tenant_id, t.slug, u.email, u.name, u.role FROM users u JOIN tenants t ON t.id = u.tenant_id WHERE u.role = 'employee'"
    )
    return {r["slug"]: CurrentUser(str(r["id"]), str(r["tenant_id"]), r["slug"], r["email"], r["name"], r["role"]) for r in rows}


async def ask(user: CurrentUser, q: str) -> dict:
    out = {"answer": "", "citations": [], "latency_ms": 0}
    async for ev in run_chat(user, q):
        if ev["type"] == "done":
            out["answer"] = ev["answer"]
            out["citations"] = ev.get("citations", [])
            out["latency_ms"] = ev["latency_ms"]
            out["usage"] = ev["usage"]
        elif ev["type"] == "citations":
            out["retrieved"] = ev["citations"]
    return out


async def evaluate(strategy: str, questions: list[dict], k: int) -> dict:
    s = get_settings()
    s.chunk_strategy = strategy
    s.retrieval_k = k
    await reset_db()
    await run_migrations()
    await create_pool()
    await create_redis()
    await get_redis().flushdb()
    from scripts.seed import seed

    t0 = time.perf_counter()
    await seed(via_queue=False)
    ingest_s = time.perf_counter() - t0
    chunks = await get_pool().fetchval("SELECT count(*) FROM chunks")
    people = await users()
    agent = LocalAgent()
    rows = []
    for item in questions:
        user = people[item["tenant"]]
        if item["type"] == "action":
            r = await agent.message(user, f"eval-{uuid.uuid4().hex[:12]}", item["q"])
            first = r["trace"][0]["name"] if r.get("trace") else None
            rows.append({**item, "ok": first == item["tool"], "got_tool": f"{first} | {r.get('text', '')[:120]}"})
            continue
        r = await ask(user, item["q"])
        retrieved_titles = [c["title"] for c in r.get("retrieved", [])]
        cited_titles = [c["title"] for c in r["citations"] if c.get("used")]
        refused = is_refusal(r["answer"])
        row = {**item, "answer": r["answer"], "latency_ms": r["latency_ms"], "retrieved": retrieved_titles, "cited": cited_titles, "refused": refused}
        if item["type"] == "answerable":
            row["hit"] = item["doc"] in retrieved_titles
            row["correct"] = (not refused) and all(norm(e) in norm(r["answer"]) for e in item["expect"])
            row["ok"] = row["correct"]
        elif item["type"] == "unanswerable":
            row["ok"] = refused
        else:
            leaked = [t for t in retrieved_titles + cited_titles if t.startswith(item["foreign"])]
            row["leaked"] = leaked
            row["ok"] = refused and not leaked
        rows.append(row)
    await close_redis()
    await close_pool()

    def subset(t):
        return [r for r in rows if r["type"] == t]

    ans = subset("answerable")
    lat = [r["latency_ms"] for r in rows if "latency_ms" in r]
    return {
        "strategy": strategy,
        "chunks": chunks,
        "ingest_s": round(ingest_s, 2),
        "retrieval_hit_rate": round(sum(r["hit"] for r in ans) / len(ans), 3),
        "answer_accuracy": round(sum(r["correct"] for r in ans) / len(ans), 3),
        "refusal_accuracy": round(sum(r["ok"] for r in subset("unanswerable")) / max(1, len(subset("unanswerable"))), 3),
        "cross_tenant_ok": f"{sum(r['ok'] for r in subset('cross_tenant'))}/{len(subset('cross_tenant'))}",
        "leaks": sum(len(r.get("leaked", [])) for r in subset("cross_tenant")),
        "tool_accuracy": round(sum(r["ok"] for r in subset("action")) / max(1, len(subset("action"))), 3),
        "p50_latency_ms": statistics.median(lat) if lat else 0,
        "rows": rows,
    }


def to_markdown(results: list[dict], backend: str, embed: str, k: int) -> str:
    lines = [
        f"# Eval results ({backend} LLM, {embed} embeddings, top-{k})",
        "",
        "| Chunking | Chunks | Retrieval hit@k | Answer accuracy | Refusal accuracy | Cross-tenant refused | Leaked chunks | Tool choice | p50 ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        lines.append(
            f"| {r['strategy']} | {r['chunks']} | {r['retrieval_hit_rate']:.0%} | {r['answer_accuracy']:.0%} | {r['refusal_accuracy']:.0%} | {r['cross_tenant_ok']} | {r['leaks']} | {r['tool_accuracy']:.0%} | {r['p50_latency_ms']} |"
        )
    lines += ["", "## Misses", ""]
    for r in results:
        for row in r["rows"]:
            if not row["ok"]:
                detail = row.get("answer", row.get("got_tool", ""))
                lines.append(f"- [{r['strategy']}] {row['id']} ({row['type']}): {row['q']} -> {str(detail)[:160]}")
    return "\n".join(lines) + "\n"


async def main() -> None:
    logging.basicConfig(level=logging.ERROR)
    p = argparse.ArgumentParser()
    p.add_argument("--strategies", default="fixed,hierarchical,semantic")
    p.add_argument("--k", type=int, default=5)
    a = p.parse_args()
    questions = json.loads((HERE / "questions.json").read_text())
    results = []
    for strategy in a.strategies.split(","):
        factory.reset()
        results.append(await evaluate(strategy, questions, a.k))
    s = get_settings()
    md = to_markdown(results, s.ai_backend, s.embed_backend, a.k)
    tag = f"{s.ai_backend}-{s.embed_backend}"
    (HERE / f"results-{tag}.md").write_text(md)
    (HERE / f"results-{tag}.json").write_text(json.dumps(results, indent=2, default=str))
    print(md)


if __name__ == "__main__":
    asyncio.run(main())
