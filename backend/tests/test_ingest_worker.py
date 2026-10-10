import uuid

import pytest

from app.infra.broker import broker
from app.providers import factory
from app.providers.base import ProviderError
from app.providers.stub import StubEmbedder
from tests.conftest import get_pool, wait_for


async def upload(client, admin, name, content: bytes, mime="text/markdown"):
    r = await client.post("/api/documents", headers=admin["headers"], files={"file": (name, content, mime)})
    assert r.status_code == 202, r.text
    return r.json()["id"]


async def doc_state(doc_id):
    return await get_pool().fetchrow("SELECT status, attempts, chunk_count, error FROM documents WHERE id = $1", uuid.UUID(doc_id))


async def queue_count(name):
    ch = await broker.connection.channel()
    try:
        q = await ch.declare_queue(name, passive=True)
        return q.declaration_result.message_count
    finally:
        await ch.close()


async def test_upload_is_processed_by_worker(client, acme_admin):
    doc_id = await upload(client, acme_admin, "parking.md", b"# Parking\n\n## Bikes\n\nTwo-wheeler parking is free in basement 2 for all employees with a badge.")

    async def ready():
        st = await doc_state(doc_id)
        return st if st["status"] == "ready" else None

    st = await wait_for(ready)
    assert st["chunk_count"] >= 1 and st["attempts"] == 1


async def test_duplicate_upload_rejected(client, acme_admin):
    body = b"# Dup\n\nThis file is uploaded twice to check dedupe works."
    await upload(client, acme_admin, "dup.md", body)
    r = await client.post("/api/documents", headers=acme_admin["headers"], files={"file": ("dup2.md", body, "text/markdown")})
    assert r.status_code == 409


async def test_unsupported_type_rejected(client, acme_admin):
    r = await client.post("/api/documents", headers=acme_admin["headers"], files={"file": ("x.exe", b"MZ....", "application/octet-stream")})
    assert r.status_code == 400


async def test_permanent_failure_goes_to_dlq(client, acme_admin):
    before = await queue_count("ingest.dlq")
    doc_id = await upload(client, acme_admin, "broken.pdf", b"this is not a pdf at all", "application/pdf")

    async def failed():
        st = await doc_state(doc_id)
        return st if st["status"] == "failed" else None

    st = await wait_for(failed)
    assert "could not read" in st["error"] and st["attempts"] == 1
    assert await wait_for(lambda: _gt(queue_count("ingest.dlq"), before))


async def _gt(coro, n):
    return (await coro) > n


class FlakyEmbedder(StubEmbedder):
    def __init__(self, failures):
        super().__init__()
        self.failures = failures
        self.calls = 0

    async def embed(self, texts, kind="document"):
        self.calls += 1
        if self.calls <= self.failures:
            raise ProviderError("ThrottlingException", retryable=True, status=429)
        return await super().embed(texts, kind)


async def test_transient_failure_is_retried_then_succeeds(client, acme_admin, monkeypatch):
    flaky = FlakyEmbedder(failures=1)
    monkeypatch.setattr(factory, "_embedder", flaky)
    monkeypatch.setattr(factory, "get_embedder", lambda: flaky)
    import app.services.ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "get_embedder", lambda: flaky)
    doc_id = await upload(client, acme_admin, "canteen.md", b"# Canteen\n\nThe canteen on floor 2 serves lunch from 12:30 to 2:30 on weekdays.")

    async def ready():
        st = await doc_state(doc_id)
        return st if st["status"] == "ready" else None

    st = await wait_for(ready, timeout=15)
    assert st["attempts"] == 2


async def test_retries_exhausted_dead_letters(client, acme_admin, monkeypatch):
    flaky = FlakyEmbedder(failures=99)
    import app.services.ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "get_embedder", lambda: flaky)
    before = await queue_count("ingest.dlq")
    doc_id = await upload(client, acme_admin, "gym.md", b"# Gym\n\nThe office gym opens at 6 AM and closes at 10 PM every day.")

    async def failed():
        st = await doc_state(doc_id)
        return st if st["status"] == "failed" else None

    st = await wait_for(failed, timeout=20)
    assert st["attempts"] == 3 and "Throttling" in st["error"]
    assert await wait_for(lambda: _gt(queue_count("ingest.dlq"), before))
    r = await client.post(f"/api/documents/{doc_id}/retry", headers=acme_admin["headers"])
    assert r.status_code == 202


async def test_delete_document_removes_chunks_and_answers(client, acme_admin, acme_employee):
    doc_id = await upload(client, acme_admin, "plants.md", b"# Office Plants\n\nEvery desk may have one small potted plant watered by the facilities team on Mondays.")

    async def ready():
        return (await doc_state(doc_id))["status"] == "ready"

    await wait_for(ready)
    q = {"question": "Who waters the potted plants on desks?"}
    d = (await client.post("/api/chat", headers=acme_employee["headers"], json=q)).json()
    assert "facilities team" in d["answer"]
    assert (await client.delete(f"/api/documents/{doc_id}", headers=acme_admin["headers"])).status_code == 204
    assert await get_pool().fetchval("SELECT count(*) FROM chunks WHERE document_id = $1", uuid.UUID(doc_id)) == 0
    d = (await client.post("/api/chat", headers=acme_employee["headers"], json=q)).json()
    assert d["cached"] is False and "facilities team" not in d["answer"]


async def test_usage_consumer_is_idempotent(services):
    from app.services.usage import insert_usage, usage_event

    tenant = await get_pool().fetchval("SELECT id FROM tenants WHERE slug = 'acme'")
    ev = usage_event(str(tenant), None, "chat", "bedrock", "us.amazon.nova-lite-v1:0", 1000, 500, 900)
    assert ev["cost_usd"] == pytest.approx(0.00018)
    assert await insert_usage(ev) is True
    assert await insert_usage(ev) is False


async def test_admin_usage_dashboard(client, acme_admin):
    r = await client.get("/api/admin/usage?days=7", headers=acme_admin["headers"])
    assert r.status_code == 200
    d = r.json()
    assert d["daily"] and d["by_model"]
    assert "ingest" in d["queues"] and d["backend"]["ai"] == "stub"
