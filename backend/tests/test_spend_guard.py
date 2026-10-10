import pytest

from app.config import get_settings
from app.infra import spend
from app.providers import factory
from app.providers.base import ProviderError
from tests.conftest import get_redis


class PaidLLM:
    backend = "bedrock"
    model = "us.amazon.nova-lite-v1:0"

    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    async def stream(self, system, messages, grounding=None):
        self.calls += 1
        if self.fail:
            raise ProviderError("ThrottlingException", retryable=True)
        from app.providers.base import StreamEvent, Usage

        yield StreamEvent("token", "Laptops are refreshed every 3 years. [1]")
        yield StreamEvent("done", usage=Usage(1000, 50))


@pytest.fixture
async def clean_spend(services):
    r = get_redis()
    for k in await r.keys("spend:*") + await r.keys("circuit:*"):
        await r.delete(k)
    yield
    for k in await r.keys("spend:*") + await r.keys("circuit:*"):
        await r.delete(k)


async def test_spend_cap_switches_to_fallback(client, fresh_employee, monkeypatch, clean_spend):
    s = get_settings()
    monkeypatch.setattr(s, "ai_backend", "bedrock")
    monkeypatch.setattr(s, "fallback_backend", "stub")
    monkeypatch.setattr(s, "aws_daily_usd_limit", 0.00005)
    paid = PaidLLM()
    factory.set_llm("bedrock", paid)
    q = {"question": "How often are laptops refreshed?"}
    first = (await client.post("/api/chat", headers=fresh_employee["headers"], json=q)).json()
    assert first["backend"] == "bedrock" and paid.calls == 1
    assert await spend.spent_today() >= 0.00005
    q2 = {"question": "How often are laptops refreshed? second"}
    second = (await client.post("/api/chat", headers=fresh_employee["headers"], json=q2)).json()
    assert second["backend"] == "stub" and second["fallback"] == "spend_cap" and paid.calls == 1


async def test_spend_cap_without_fallback_refuses(client, fresh_employee, monkeypatch, clean_spend):
    s = get_settings()
    monkeypatch.setattr(s, "ai_backend", "bedrock")
    monkeypatch.setattr(s, "fallback_backend", "none")
    monkeypatch.setattr(s, "aws_daily_usd_limit", 0.0)
    factory.set_llm("bedrock", PaidLLM())
    r = await client.post("/api/chat", headers=fresh_employee["headers"], json={"question": "What is the wellness allowance?"})
    assert r.status_code == 429


async def test_circuit_opens_after_repeated_failures(client, fresh_employee, monkeypatch, clean_spend):
    s = get_settings()
    monkeypatch.setattr(s, "ai_backend", "bedrock")
    monkeypatch.setattr(s, "fallback_backend", "stub")
    monkeypatch.setattr(s, "circuit_max_failures", 3)
    failing = PaidLLM(fail=True)
    factory.set_llm("bedrock", failing)
    for i in range(3):
        d = (await client.post("/api/chat", headers=fresh_employee["headers"], json={"question": f"How often are laptops refreshed {i}?"})).json()
        assert d["answer"]
    assert failing.calls == 3
    assert (await spend.status())["bedrock_paused"] is True
    d = (await client.post("/api/chat", headers=fresh_employee["headers"], json={"question": "How often are laptops refreshed again?"})).json()
    assert d["fallback"] == "circuit_open" and failing.calls == 3


async def test_aws_kill_switch(monkeypatch, clean_spend):
    monkeypatch.setattr(get_settings(), "aws_enabled", False)
    assert await spend.blocked_reason("bedrock") == "aws_disabled"
    assert await spend.blocked_reason("groq") is None


async def test_titan_refuses_when_paused(monkeypatch, clean_spend):
    from app.providers.bedrock import TitanEmbedder

    monkeypatch.setattr(get_settings(), "aws_daily_usd_limit", 0.0)
    with pytest.raises(ProviderError) as e:
        await TitanEmbedder().embed(["x"])
    assert not e.value.retryable


def test_context_is_capped():
    from app.rag.prompts import build_context
    from app.rag.retriever import Hit

    hits = [Hit(str(i), "d", "Doc", None, "word " * 2000, None, 0.1, 0.5, "pgvector") for i in range(5)]
    assert len(build_context(hits, max_chars=3000)) <= 3000
