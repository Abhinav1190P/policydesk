import asyncpg
from pgvector.asyncpg import register_vector

from app.config import get_settings

_pool: asyncpg.Pool | None = None


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    await register_vector(conn)


async def create_pool(dsn: str | None = None) -> asyncpg.Pool:
    global _pool
    settings = get_settings()
    _pool = await asyncpg.create_pool(
        dsn or settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        init=_init_conn,
        command_timeout=30,
    )
    return _pool


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialised")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
