import argparse
import asyncio
import json
import os
import statistics
import time
from pathlib import Path

import asyncpg

DSN = os.environ.get("BENCH_DATABASE_URL", "postgresql://policydesk:policydesk@localhost:5432/policydesk_bench")
OUT = Path(__file__).resolve().parents[1] / "bench"
TENANTS = 200
MONTHS = 12
START = "2025-10-01"

COLUMNS = """
    event_id UUID NOT NULL,
    tenant_id INTEGER NOT NULL,
    user_id INTEGER,
    kind TEXT NOT NULL,
    backend TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd NUMERIC(14,8) NOT NULL,
    latency_ms INTEGER,
    cached BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
"""

FILL = """
INSERT INTO {table}
SELECT gen_random_uuid(),
       (1 + floor(power(random(), 2) * {tenants}))::int,
       (random() * 50000)::int,
       (ARRAY['chat','chat','chat','embed','agent'])[1 + (g % 5)],
       (ARRAY['groq','bedrock','bedrock','stub'])[1 + (g % 4)],
       'us.amazon.nova-lite-v1:0',
       200 + (random() * 3000)::int,
       20 + (random() * 600)::int,
       (random() * 0.002)::numeric(14,8),
       100 + (random() * 3000)::int,
       random() < 0.3,
       timestamptz '{start}' + ((g - 1)::double precision / {rows}) * interval '{months} months'
FROM generate_series({lo}, {hi}) g
"""

QUERIES = {
    "Q1 tenant monthly cost": (
        "SELECT sum(cost_usd), sum(input_tokens + output_tokens) FROM {t} WHERE tenant_id = $1 AND created_at >= $2 AND created_at < $2 + interval '1 month'",
        lambda: (37, "2026-06-01"),
    ),
    "Q2 tenant latest 50 rows": (
        "SELECT event_id, kind, cost_usd, created_at FROM {t} WHERE tenant_id = $1 ORDER BY created_at DESC LIMIT 50",
        lambda: (37,),
    ),
    "Q3 platform 1-day totals": (
        "SELECT backend, count(*), sum(cost_usd) FROM {t} WHERE created_at >= $1 AND created_at < $1 + interval '1 day' GROUP BY backend",
        lambda: ("2026-06-15",),
    ),
}


async def fill(conn, table: str, rows: int, batch: int) -> float:
    started = time.perf_counter()
    for lo in range(1, rows + 1, batch):
        hi = min(rows, lo + batch - 1)
        await conn.execute(FILL.format(table=table, tenants=TENANTS, start=START, rows=rows, months=MONTHS, lo=lo, hi=hi))
    return time.perf_counter() - started


async def setup(conn, rows: int) -> dict:
    timings = {}
    await conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    await conn.execute("DROP TABLE IF EXISTS bench_flat, bench_part CASCADE")
    await conn.execute(f"CREATE TABLE bench_flat ({COLUMNS})")
    await conn.execute(f"CREATE TABLE bench_part ({COLUMNS}) PARTITION BY RANGE (created_at)")
    for i in range(MONTHS):
        await conn.execute(
            f"CREATE TABLE bench_part_{i:02d} PARTITION OF bench_part FOR VALUES FROM (timestamptz '{START}' + interval '{i} months') TO (timestamptz '{START}' + interval '{i + 1} months')"
        )
    await conn.execute("CREATE INDEX ON bench_part (tenant_id, created_at)")
    timings["fill_flat_s"] = await fill(conn, "bench_flat", rows, 1_000_000)
    timings["fill_part_s"] = await fill(conn, "bench_part", rows, 1_000_000)
    await conn.execute("VACUUM ANALYZE bench_flat")
    await conn.execute("VACUUM ANALYZE bench_part")
    return timings


async def measure(conn, sql: str, args: tuple, runs: int) -> dict:
    plan_sql = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql
    await conn.fetchval(plan_sql, *args)
    times, plan = [], None
    for _ in range(runs):
        raw = await conn.fetchval(plan_sql, *args)
        plan = json.loads(raw)[0] if isinstance(raw, str) else raw[0]
        times.append(plan["Execution Time"])
    root = plan["Plan"]

    def nodes(n):
        yield n
        for c in n.get("Plans", []):
            yield from nodes(c)

    kinds = sorted({n["Node Type"] for n in nodes(root) if "Scan" in n["Node Type"]})
    scanned = sorted({n.get("Relation Name") for n in nodes(root) if n.get("Relation Name")})
    return {
        "median_ms": round(statistics.median(times), 3),
        "shared_hit": root.get("Shared Hit Blocks", 0),
        "shared_read": root.get("Shared Read Blocks", 0),
        "scans": kinds,
        "relations": len(scanned),
    }


async def size(conn, rel: str) -> int:
    return await conn.fetchval("SELECT pg_relation_size($1::regclass)", rel)


async def run(rows: int, runs: int) -> dict:
    conn = await asyncpg.connect(DSN)
    await conn.execute("SET max_parallel_workers_per_gather = 0")
    await conn.execute("SET maintenance_work_mem = '512MB'")
    result: dict = {"rows": rows, "tenants": TENANTS, "months": MONTHS}
    result["setup"] = await setup(conn, rows)
    await conn.execute("DROP INDEX IF EXISTS bench_flat_btree, bench_flat_brin")
    variants = {}

    async def run_all(label: str, table: str):
        variants[label] = {}
        for name, (sql, args) in QUERIES.items():
            variants[label][name] = await measure(conn, sql.format(t=table), tuple(_coerce(a) for a in args()), runs)

    await run_all("flat, no index", "bench_flat")
    t0 = time.perf_counter()
    await conn.execute("CREATE INDEX bench_flat_btree ON bench_flat (tenant_id, created_at)")
    build_btree = time.perf_counter() - t0
    t0 = time.perf_counter()
    await conn.execute("CREATE INDEX bench_flat_brin ON bench_flat USING brin (created_at) WITH (pages_per_range = 32)")
    build_brin = time.perf_counter() - t0
    await conn.execute("ANALYZE bench_flat")
    await run_all("flat + btree(tenant_id, created_at) + brin(created_at)", "bench_flat")
    await conn.execute("DROP INDEX bench_flat_brin")
    await run_all("flat + btree only", "bench_flat")
    await run_all("monthly partitions + local btree", "bench_part")
    result["variants"] = variants
    part_idx = await conn.fetchval(
        "SELECT sum(pg_relation_size(i.indexrelid)) FROM pg_index i JOIN pg_inherits h ON h.inhrelid = i.indrelid WHERE h.inhparent = 'bench_part'::regclass"
    )
    await conn.execute("CREATE INDEX bench_flat_brin ON bench_flat USING brin (created_at) WITH (pages_per_range = 32)")
    result["sizes_mb"] = {
        "flat_table": round(await size(conn, "bench_flat") / 1e6, 1),
        "flat_btree": round(await size(conn, "bench_flat_btree") / 1e6, 1),
        "flat_brin": round(await size(conn, "bench_flat_brin") / 1e6, 3),
        "partition_btrees_total": round(float(part_idx) / 1e6, 1),
    }
    result["index_build_s"] = {"btree": round(build_btree, 2), "brin": round(build_brin, 2)}
    tr = conn.transaction()
    await tr.start()
    t0 = time.perf_counter()
    deleted = await conn.execute(f"DELETE FROM bench_flat WHERE created_at < timestamptz '{START}' + interval '1 month'")
    delete_s = time.perf_counter() - t0
    await tr.rollback()
    t0 = time.perf_counter()
    await conn.execute("ALTER TABLE bench_part DETACH PARTITION bench_part_00")
    await conn.execute("DROP TABLE bench_part_00")
    drop_s = time.perf_counter() - t0
    result["retention"] = {"delete_one_month_s": round(delete_s, 3), "deleted": deleted, "detach_drop_partition_s": round(drop_s, 3)}
    await conn.close()
    return result


def _coerce(a):
    if isinstance(a, str) and a[:2] == "20":
        from datetime import datetime, timezone

        return datetime.fromisoformat(a).replace(tzinfo=timezone.utc)
    return a


def to_markdown(r: dict) -> str:
    lines = [f"# usage_ledger benchmark ({r['rows']:,} rows, {r['tenants']} tenants, {r['months']} months)", ""]
    lines.append("| Variant | Query | Median ms | Buffers (hit/read) | Scan nodes | Relations touched |")
    lines.append("|---|---|---:|---:|---|---:|")
    for v, qs in r["variants"].items():
        for q, m in qs.items():
            lines.append(f"| {v} | {q} | {m['median_ms']} | {m['shared_hit']}/{m['shared_read']} | {', '.join(m['scans'])} | {m['relations']} |")
    lines += ["", "| Object | Size MB |", "|---|---:|"] + [f"| {k} | {v} |" for k, v in r["sizes_mb"].items()]
    lines += ["", f"Index build: btree {r['index_build_s']['btree']} s, brin {r['index_build_s']['brin']} s", ""]
    ret = r["retention"]
    lines.append(f"Retention: DELETE one month on flat table {ret['delete_one_month_s']} s ({ret['deleted']}), DETACH + DROP one partition {ret['detach_drop_partition_s']} s")
    return "\n".join(lines) + "\n"


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--rows", type=int, default=10_000_000)
    p.add_argument("--runs", type=int, default=5)
    a = p.parse_args()
    r = await run(a.rows, a.runs)
    OUT.mkdir(exist_ok=True)
    (OUT / "usage_results.json").write_text(json.dumps(r, indent=2, default=str))
    (OUT / "usage_results.md").write_text(to_markdown(r))
    print(to_markdown(r))


if __name__ == "__main__":
    asyncio.run(main())
