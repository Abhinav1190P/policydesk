import asyncio
import logging
from pathlib import Path

import asyncpg

from app.config import get_settings

log = logging.getLogger("policydesk.migrate")
MIGRATIONS_DIR = Path(__file__).parent / "migrations"
LOCK_KEY = 551177


async def run_migrations(dsn: str | None = None) -> list[str]:
    conn = await asyncpg.connect(dsn or get_settings().database_url)
    applied: list[str] = []
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", LOCK_KEY)
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            async with conn.transaction():
                await conn.execute(path.read_text())
                await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", path.name)
            applied.append(path.name)
            log.info("applied migration %s", path.name)
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_KEY)
        await conn.close()
    return applied


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(asyncio.run(run_migrations()))
