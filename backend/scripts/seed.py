import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.migrate import run_migrations
from app.db.pool import close_pool, create_pool, get_pool
from app.infra.broker import broker
from app.infra.redis_client import close_redis, create_redis
from app.security import hash_password
from app.services import ingest

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "Password123!"

TENANTS = [
    {
        "slug": "acme",
        "name": "Acme Cloud",
        "invite": "ACME-2026",
        "docs": "acme",
        "users": [
            ("admin@acme.example", "Asha Rao", "admin", (24, 3, 12, 0)),
            ("employee@acme.example", "Priya Nair", "employee", (24, 9.5, 12, 2)),
        ],
    },
    {
        "slug": "globex",
        "name": "Globex Manufacturing",
        "invite": "GLOBEX-2026",
        "docs": "globex",
        "users": [
            ("admin@globex.example", "Vikram Joshi", "admin", (18, 2, 7, 0)),
            ("employee@globex.example", "Rahul Patil", "employee", (18, 4, 7, 1)),
        ],
    },
]

CATEGORIES = {"leave": "hr", "remote": "hr", "benefits": "hr", "shift": "hr", "travel": "finance", "it": "it"}


def category_for(name: str) -> str:
    for prefix, cat in CATEGORIES.items():
        if name.startswith(prefix):
            return cat
    return "general"


async def seed(via_queue: bool) -> None:
    pool = get_pool()
    for t in TENANTS:
        tid = await pool.fetchval(
            "INSERT INTO tenants (slug, name, invite_code) VALUES ($1, $2, $3) ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name RETURNING id",
            t["slug"], t["name"], t["invite"],
        )
        admin_id = None
        for email, name, role, (at, au, st, su) in t["users"]:
            uid = await pool.fetchval("SELECT id FROM users WHERE lower(email) = $1", email)
            if uid is None:
                uid = await pool.fetchval(
                    "INSERT INTO users (tenant_id, email, name, password_hash, role) VALUES ($1, $2, $3, $4, $5) RETURNING id",
                    tid, email, name, hash_password(PASSWORD), role,
                )
            await pool.execute(
                """
                INSERT INTO leave_balances (user_id, tenant_id, annual_total, annual_used, sick_total, sick_used)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (user_id) DO UPDATE SET annual_total = $3, annual_used = $4, sick_total = $5, sick_used = $6
                """,
                uid, tid, at, au, st, su,
            )
            if role == "admin":
                admin_id = uid
        for path in sorted((ROOT / "sample_docs" / t["docs"]).glob("*.md")):
            title = path.read_text().splitlines()[0].lstrip("# ").strip()
            try:
                doc = await ingest.create_document(str(tid), str(admin_id), path.name, "text/markdown", path.read_bytes(), title, category_for(path.stem), publish=via_queue)
            except ingest.UploadError as e:
                print(f"  skip {t['slug']}/{path.name}: {e}")
                continue
            if not via_queue:
                result = await ingest.process_document(doc["id"])
                print(f"  {t['slug']}/{path.name}: {result}")
            else:
                print(f"  {t['slug']}/{path.name}: queued")
    print(f"seeded. password for all users: {PASSWORD}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--via-queue", action="store_true")
    args = parser.parse_args()
    await run_migrations()
    await create_pool()
    await create_redis()
    if args.via_queue:
        await broker.connect()
    try:
        await seed(args.via_queue)
    finally:
        await broker.close()
        await close_redis()
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
