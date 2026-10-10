import argparse
import asyncio
import json
import os
import random
import statistics
import time
from pathlib import Path

import asyncpg
import numpy as np
from pgvector.asyncpg import register_vector

DSN = os.environ.get("BENCH_DATABASE_URL", "postgresql://policydesk:policydesk@localhost:5432/policydesk_bench")
OUT = Path(__file__).resolve().parents[1] / "bench"


def make_data(n: int, dim: int, clusters: int, tenants: int, seed: int = 7):
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(clusters, dim)).astype(np.float32)
    assign = rng.integers(0, clusters, size=n)
    x = centers[assign] + 0.6 * rng.normal(size=(n, dim)).astype(np.float32)
    x /= np.linalg.norm(x, axis=1, keepdims=True)
    tenant = rng.integers(1, tenants + 1, size=n)
    return x, tenant, centers


def make_queries(centers, count: int, dim: int, seed: int = 11):
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(centers), size=count)
    q = centers[idx] + 0.6 * rng.normal(size=(count, dim)).astype(np.float32)
    q /= np.linalg.norm(q, axis=1, keepdims=True)
    return q


async def load(conn, x, tenant):
    await conn.execute("DROP TABLE IF EXISTS bench_vec")
    await conn.execute(f"CREATE TABLE bench_vec (id BIGINT PRIMARY KEY, tenant_id INT NOT NULL, embedding vector({x.shape[1]}) NOT NULL)")
    t0 = time.perf_counter()
    await conn.copy_records_to_table("bench_vec", records=[(i, int(tenant[i]), x[i]) for i in range(len(x))], columns=["id", "tenant_id", "embedding"])
    return time.perf_counter() - t0


async def search(conn, q, k: int, tenant: int | None):
    if tenant is None:
        rows = await conn.fetch("SELECT id FROM bench_vec ORDER BY embedding <=> $1 LIMIT $2", q, k)
    else:
        rows = await conn.fetch("SELECT id FROM bench_vec WHERE tenant_id = $3 ORDER BY embedding <=> $1 LIMIT $2", q, k, tenant)
    return [r["id"] for r in rows]


async def filtered_plan(conn, q, k, tenant):
    raw = await conn.fetchval("EXPLAIN (FORMAT JSON) SELECT id FROM bench_vec WHERE tenant_id = $3 ORDER BY embedding <=> $1 LIMIT $2", q, k, tenant)
    plan = json.loads(raw)[0]["Plan"] if isinstance(raw, str) else raw[0]["Plan"]

    def walk(n):
        yield n
        for c in n.get("Plans", []):
            yield from walk(c)

    return "hnsw" if any(n.get("Index Name") == "bench_vec_hnsw" for n in walk(plan)) else "exact"


async def timed_batch(conn, queries, k, tenants):
    lat, results = [], []
    for q, t in zip(queries, tenants):
        t0 = time.perf_counter()
        results.append(await search(conn, q, k, t))
        lat.append((time.perf_counter() - t0) * 1000)
    return results, lat


def recall(truth, got, k):
    return statistics.mean(len(set(a[:k]) & set(b[:k])) / k for a, b in zip(truth, got))


def pct(values, p):
    s = sorted(values)
    return round(s[min(len(s) - 1, int(len(s) * p))], 2)


async def run(n: int, dim: int, nq: int, k: int, tenants: int) -> dict:
    conn = await asyncpg.connect(DSN)
    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    await register_vector(conn)
    await conn.execute("SET max_parallel_workers_per_gather = 0")
    x, tenant, centers = make_data(n, dim, clusters=max(50, n // 400), tenants=tenants)
    queries = list(make_queries(centers, nq, dim))
    rng = random.Random(3)
    q_tenants = [rng.randint(1, tenants) for _ in queries]
    result: dict = {"n": n, "dim": dim, "queries": nq, "k": k, "tenants": tenants}
    result["load_s"] = round(await load(conn, x, tenant), 2)
    await conn.execute("ANALYZE bench_vec")

    exact_all, lat_exact = await timed_batch(conn, queries, k, [None] * nq)
    exact_tenant, lat_exact_t = await timed_batch(conn, queries, k, q_tenants)
    result["exact"] = {"p50_ms": pct(lat_exact, 0.5), "p95_ms": pct(lat_exact, 0.95), "filtered_p50_ms": pct(lat_exact_t, 0.5)}

    await conn.execute("SET maintenance_work_mem = '1GB'")
    t0 = time.perf_counter()
    await conn.execute("CREATE INDEX bench_vec_hnsw ON bench_vec USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)")
    result["hnsw_build_s"] = round(time.perf_counter() - t0, 1)
    result["hnsw_size_mb"] = round(await conn.fetchval("SELECT pg_relation_size('bench_vec_hnsw')") / 1e6, 1)
    result["table_size_mb"] = round(await conn.fetchval("SELECT pg_total_relation_size('bench_vec') - pg_relation_size('bench_vec_hnsw')") / 1e6, 1)

    sweep = []
    for ef in (10, 20, 40, 80, 160, 320):
        await conn.execute(f"SET hnsw.ef_search = {ef}")
        await conn.execute("SET hnsw.iterative_scan = off")
        got, lat = await timed_batch(conn, queries, k, [None] * nq)
        natural_plan = await filtered_plan(conn, queries[0], k, q_tenants[0])
        await conn.execute("SET enable_seqscan = off")
        await conn.execute("SET enable_bitmapscan = off")
        forced_plan = await filtered_plan(conn, queries[0], k, q_tenants[0])
        got_t, lat_t = await timed_batch(conn, queries, k, q_tenants)
        await conn.execute("SET hnsw.iterative_scan = relaxed_order")
        got_ti, lat_ti = await timed_batch(conn, queries, k, q_tenants)
        await conn.execute("RESET enable_seqscan")
        await conn.execute("RESET enable_bitmapscan")
        await conn.execute("SET hnsw.iterative_scan = off")
        sweep.append(
            {
                "ef_search": ef,
                "recall": round(recall(exact_all, got, k), 4),
                "p50_ms": pct(lat, 0.5),
                "p95_ms": pct(lat, 0.95),
                "filtered_natural_plan": natural_plan,
                "filtered_forced_plan": forced_plan,
                "filtered_recall": round(recall(exact_tenant, got_t, k), 4),
                "filtered_avg_returned": round(statistics.mean(len(g) for g in got_t), 2),
                "filtered_iter_recall": round(recall(exact_tenant, got_ti, k), 4),
                "filtered_iter_p95_ms": pct(lat_ti, 0.95),
            }
        )
    result["sweep"] = sweep
    await conn.close()
    return result


def to_markdown(r: dict) -> str:
    lines = [
        f"# pgvector HNSW benchmark ({r['n']:,} vectors, {r['dim']} dims, {r['queries']} queries, recall@{r['k']}, {r['tenants']} tenants)",
        "",
        f"Exact scan: p50 {r['exact']['p50_ms']} ms, p95 {r['exact']['p95_ms']} ms; with tenant filter p50 {r['exact']['filtered_p50_ms']} ms",
        f"HNSW (m=16, ef_construction=64) build {r['hnsw_build_s']} s, index {r['hnsw_size_mb']} MB, table {r['table_size_mb']} MB",
        "",
        "| ef_search | recall@k | p50 ms | p95 ms | planner's choice with tenant filter | filtered recall via HNSW, iterative off | avg rows returned | filtered recall via HNSW, iterative on | p95 ms (iterative on) |",
        "|---:|---:|---:|---:|---|---:|---:|---:|---:|",
    ]
    for s in r["sweep"]:
        lines.append(
            f"| {s['ef_search']} | {s['recall']} | {s['p50_ms']} | {s['p95_ms']} | {s['filtered_natural_plan']} | {s['filtered_recall']} | {s['filtered_avg_returned']} | {s['filtered_iter_recall']} | {s['filtered_iter_p95_ms']} |"
        )
    return "\n".join(lines) + "\n"


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=100_000)
    p.add_argument("--dim", type=int, default=1024)
    p.add_argument("--queries", type=int, default=200)
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--tenants", type=int, default=50)
    a = p.parse_args()
    r = await run(a.n, a.dim, a.queries, a.k, a.tenants)
    OUT.mkdir(exist_ok=True)
    (OUT / "vector_results.json").write_text(json.dumps(r, indent=2))
    (OUT / "vector_results.md").write_text(to_markdown(r))
    print(to_markdown(r))


if __name__ == "__main__":
    asyncio.run(main())
