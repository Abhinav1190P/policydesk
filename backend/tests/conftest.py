import os

os.environ.update(
    {
        "DATABASE_URL": os.environ.get("TEST_DATABASE_URL", "postgresql://policydesk:policydesk@localhost:5432/policydesk_test"),
        "REDIS_URL": os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15"),
        "RABBITMQ_URL": os.environ.get("TEST_RABBITMQ_URL", "amqp://guest:guest@localhost:5672/policydesk_test"),
        "AI_BACKEND": "stub",
        "EMBED_BACKEND": "stub",
        "RETRIEVER": "pgvector",
        "AGENT_MODE": "local",
        "FALLBACK_BACKEND": "none",
        "INGEST_RETRY_DELAY_MS": "200",
        "RATE_LIMIT_PER_MINUTE": "6000",
        "RATE_LIMIT_BURST": "1000",
        "TENANT_RATE_LIMIT_PER_MINUTE": "60000",
        "LOGIN_ATTEMPTS_PER_MINUTE": "1000",
        "JWT_SECRET": "test-secret-test-secret-test-secret",
        "INTERNAL_TOOLS_SECRET": "internal-test-secret",
    }
)

import asyncio
import uuid

import asyncpg
import httpx
import pytest

from app.config import get_settings
from app.db.migrate import run_migrations
from app.db.pool import close_pool, create_pool, get_pool
from app.infra import ratelimit
from app.infra.broker import broker
from app.infra.redis_client import close_redis, create_redis, get_redis
from app.main import create_app
from app.providers import factory
from app.worker import Consumers

QUEUES = ["ingest", "ingest.dlq", "usage", "usage.dlq", "ingest.retry.1", "ingest.retry.2", "usage.retry.1", "usage.retry.2"]


async def _reset_db(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    finally:
        await conn.close()


@pytest.fixture(scope="session")
async def services():
    s = get_settings()
    await _reset_db(s.database_url)
    await run_migrations()
    await create_pool()
    r = await create_redis()
    await r.flushdb()
    ratelimit.reset_script_cache()
    await broker.connect()
    for q in QUEUES:
        await (await broker.channel.declare_queue(q, passive=True)).purge()
    consumers = Consumers()
    await consumers.start(broker)
    from scripts.seed import seed

    await seed(via_queue=False)
    yield {"consumers": consumers}
    await consumers.stop()
    await broker.close()
    await close_redis()
    await close_pool()


@pytest.fixture(scope="session")
async def client(services):
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        yield c


@pytest.fixture(autouse=True)
async def _reset_providers():
    factory.reset()
    yield
    factory.reset()


async def login(client, email: str, password: str = "Password123!") -> dict:
    r = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    data = r.json()
    return {"token": data["token"], "user": data["user"], "headers": {"Authorization": f"Bearer {data['token']}"}}


@pytest.fixture
async def acme_employee(client):
    return await login(client, "employee@acme.example")


@pytest.fixture
async def acme_admin(client):
    return await login(client, "admin@acme.example")


@pytest.fixture
async def globex_employee(client):
    return await login(client, "employee@globex.example")


@pytest.fixture
async def fresh_employee(client):
    email = f"user-{uuid.uuid4().hex[:8]}@acme.example"
    r = await client.post(
        "/api/auth/register",
        json={"email": email, "password": "Password123!", "name": "Test User", "tenant_slug": "acme", "invite_code": "ACME-2026"},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    return {"token": data["token"], "user": data["user"], "headers": {"Authorization": f"Bearer {data['token']}"}}


async def wait_for(predicate, timeout: float = 10.0, interval: float = 0.1):
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        result = await predicate()
        if result:
            return result
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(interval)


__all__ = ["login", "wait_for", "get_pool", "get_redis"]
