import json

import pytest

from app.config import get_settings
from app.infra import cache
from app.providers import factory
from app.providers.base import ProviderError, StreamEvent, Usage
from app.rag.prompts import NOT_FOUND
from tests.conftest import get_pool, wait_for


async def ask(client, who, question, conversation_id=None):
    body = {"question": question}
    if conversation_id:
        body["conversation_id"] = conversation_id
    r = await client.post("/api/chat", headers=who["headers"], json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def test_answer_with_citations(client, acme_employee):
    d = await ask(client, acme_employee, "How many days of paid annual leave do I get per year?")
    assert "24 days" in d["answer"]
    used = [c for c in d["citations"] if c["used"]]
    assert used and used[0]["title"] == "Acme Cloud Leave Policy"
    assert d["backend"] == "stub" and d["cached"] is False


async def test_same_question_differs_per_tenant(client, acme_employee, globex_employee):
    a = await ask(client, acme_employee, "How much sick leave do employees get each year?")
    g = await ask(client, globex_employee, "How much sick leave do employees get each year?")
    assert "12 days" in a["answer"] and "7 days" in g["answer"]


async def test_no_cross_tenant_leak(client, acme_employee):
    d = await ask(client, acme_employee, "What is the night shift differential percentage?")
    assert d["answer"] == NOT_FOUND
    titles = {c["title"] for c in d["citations"]}
    assert not any("Globex" in t for t in titles)


async def test_unanswerable_skips_llm(client, acme_employee):
    d = await ask(client, acme_employee, "zzqx flurb wobble")
    assert d["answer"] == NOT_FOUND and d["usage"] == {"input_tokens": 0, "output_tokens": 0}


async def test_cache_hit_then_invalidated_by_new_document(client, acme_employee, acme_admin):
    q = "What is the international per diem when travelling?"
    first = await ask(client, acme_employee, q)
    second = await ask(client, acme_employee, "what is the INTERNATIONAL per diem when travelling")
    assert first["cached"] is False and second["cached"] is True
    assert second["answer"] == first["answer"]
    files = {"file": ("bonus.md", b"# Acme Bonus Policy\n\nThe annual bonus is paid every April to all eligible employees.", "text/markdown")}
    r = await client.post("/api/documents", headers=acme_admin["headers"], files=files)
    assert r.status_code == 202
    doc_id = r.json()["id"]

    async def ready():
        d = (await client.get(f"/api/documents/{doc_id}", headers=acme_admin["headers"])).json()
        return d["status"] == "ready"

    await wait_for(ready)
    third = await ask(client, acme_employee, q)
    assert third["cached"] is False


async def test_follow_up_uses_same_conversation(client, acme_employee):
    d1 = await ask(client, acme_employee, "What is the hotel cap per night in metro cities?")
    d2 = await ask(client, acme_employee, "And in other Indian cities, what is the hotel cap per night?", d1["conversation_id"])
    assert d2["conversation_id"] == d1["conversation_id"] and d2["cached"] is False
    r = await client.get(f"/api/conversations/{d1['conversation_id']}/messages", headers=acme_employee["headers"])
    assert [m["role"] for m in r.json()] == ["user", "assistant", "user", "assistant"]


async def test_other_user_cannot_read_conversation(client, acme_employee, fresh_employee):
    d = await ask(client, acme_employee, "What are the core hours?")
    r = await client.get(f"/api/conversations/{d['conversation_id']}/messages", headers=fresh_employee["headers"])
    assert r.status_code == 404
    r = await client.post("/api/chat", headers=fresh_employee["headers"], json={"question": "hi", "conversation_id": d["conversation_id"]})
    assert r.status_code == 404


async def test_sse_stream_event_order(client, acme_employee):
    events = []
    async with client.stream("POST", "/api/chat/stream", headers=acme_employee["headers"], json={"question": "How long must passwords be?"}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    types = [e["type"] for e in events]
    assert types[0] == "meta" and types[1] == "citations" and types[-1] == "done"
    assert "token" in types
    streamed = "".join(e["text"] for e in events if e["type"] == "token")
    assert streamed.strip() == events[-1]["answer"]
    assert "14 characters" in streamed


async def test_usage_lands_in_partitioned_ledger(client, fresh_employee):
    await ask(client, fresh_employee, "What is the learning budget per year?")

    async def landed():
        return await get_pool().fetchval(
            "SELECT count(*) FROM usage_ledger WHERE user_id = $1 AND kind = 'chat'", __import__("uuid").UUID(fresh_employee["user"]["id"])
        )

    assert await wait_for(landed) == 1
    part = await get_pool().fetchval(
        "SELECT tableoid::regclass::text FROM usage_ledger WHERE user_id = $1 LIMIT 1", __import__("uuid").UUID(fresh_employee["user"]["id"])
    )
    assert part.startswith("usage_ledger_20")


class FailingLLM:
    backend = "groq"
    model = "failing"

    async def stream(self, system, messages, grounding=None):
        raise ProviderError("rate limited", retryable=True, status=429)
        yield

    async def chat_with_tools(self, *a, **k):
        raise ProviderError("down", retryable=True)


async def test_falls_back_when_primary_backend_fails(client, fresh_employee, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "ai_backend", "groq")
    monkeypatch.setattr(s, "fallback_backend", "stub")
    factory.set_llm("groq", FailingLLM())
    d = await ask(client, fresh_employee, "How often are laptops refreshed?")
    assert d["answer"] != "" and "3 years" in d["answer"]


async def test_budget_exhausted_without_fallback_returns_429(client, fresh_employee, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "daily_token_budget", 1)
    await cache.add_tokens(fresh_employee["user"]["tenant_id"], 5)
    r = await client.post("/api/chat", headers=fresh_employee["headers"], json={"question": "What is the commute allowance?"})
    assert r.status_code == 429


async def test_budget_exhausted_switches_to_fallback(client, fresh_employee, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "daily_token_budget", 1)
    monkeypatch.setattr(s, "ai_backend", "groq")
    monkeypatch.setattr(s, "fallback_backend", "stub")
    factory.set_llm("groq", FailingLLM())
    await cache.add_tokens(fresh_employee["user"]["tenant_id"], 5)
    d = await ask(client, fresh_employee, "What is the gym allowance per month?")
    assert d["backend"] == "stub" and d["fallback"] == "budget"


async def test_chat_rate_limit(client, fresh_employee, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "rate_limit_per_minute", 2)
    monkeypatch.setattr(s, "rate_limit_burst", 2)
    codes = [(await client.post("/api/chat", headers=fresh_employee["headers"], json={"question": f"core hours {i}?"})).status_code for i in range(3)]
    assert codes == [200, 200, 429]


async def test_global_budget_caps_all_tenants(client, fresh_employee, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "global_daily_token_budget", 1)
    await cache.add_tokens("00000000-0000-0000-0000-000000000000", 5)
    r = await client.post("/api/chat", headers=fresh_employee["headers"], json={"question": "What is the parking policy?"})
    assert r.status_code == 429


async def test_ip_rate_limit_spans_accounts(client, acme_employee, globex_employee, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "ip_rate_limit_per_minute", 3)
    ip = {"cf-connecting-ip": "203.0.113.77"}
    codes = []
    for i, who in enumerate([acme_employee, globex_employee, acme_employee, globex_employee]):
        r = await client.post("/api/chat", headers={**who["headers"], **ip}, json={"question": f"dress code {i}?"})
        codes.append(r.status_code)
    assert codes == [200, 200, 200, 429]
