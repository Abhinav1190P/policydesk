import pytest

from app.infra import ratelimit
from app.providers.stub import StubEmbedder, StubLLM, extractive_answer
from app.providers.base import ToolSpec
from app.rag.prompts import NOT_FOUND
from app.services.guard import allowed_contacts, mask_pii

CONTEXT = "[1] Leave\nEmployees get 24 days of paid annual leave per year.\n\n[2] IT\nReport lost laptops to it-help@acme.example within 2 hours."


def test_extractive_answer_cites_source():
    out = extractive_answer(CONTEXT, "How many annual leave days do employees get?")
    assert "24 days" in out and "[1]" in out


def test_extractive_answer_refuses_unrelated():
    assert extractive_answer(CONTEXT, "What is the night shift differential?") == NOT_FOUND


def test_mask_pii_masks_but_keeps_allowed_contacts():
    text = "Mail it-help@acme.example or call me on 98765 43210, my card 4111 1111 1111 1111, friend bob@gmail.com"
    out, changed = mask_pii(text, allowed_contacts(CONTEXT))
    assert changed
    assert "it-help@acme.example" in out
    assert "bob@gmail.com" not in out and "[EMAIL]" in out
    assert "4111" not in out and "98765" not in out


def test_mask_pii_noop_on_clean_text():
    out, changed = mask_pii("You get 24 days of leave [1].")
    assert not changed and out == "You get 24 days of leave [1]."


async def test_stub_embedder_is_deterministic_and_normalized():
    e = StubEmbedder()
    (a, b), _ = await e.embed(["annual leave policy", "annual leave policy"])
    assert a == b
    assert abs(sum(x * x for x in a) - 1.0) < 1e-6
    assert len(a) == 1024


async def test_stub_tool_selection():
    llm = StubLLM()
    tools = [ToolSpec(n, "", {}) for n in ("search_policies", "get_leave_balance", "create_it_ticket")]
    r = await llm.chat_with_tools("", [{"role": "user", "content": "how many leave days do I have left"}], tools)
    assert r.tool_calls[0].name == "get_leave_balance"
    r = await llm.chat_with_tools("", [{"role": "user", "content": "VPN is down, I am blocked"}], tools)
    assert r.tool_calls[0].name == "create_it_ticket" and r.tool_calls[0].args["category"] == "network"
    assert r.tool_calls[0].args["priority"] == "high"
    r = await llm.chat_with_tools("", [{"role": "user", "content": "what is the travel per diem"}], tools)
    assert r.tool_calls[0].name == "search_policies"


async def test_token_bucket_limits_and_refills(services):
    key = "test:bucket"
    results = [await ratelimit.take(key, per_minute=60, burst=3) for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    assert results[-1].retry_after_ms > 0
    import asyncio

    await asyncio.sleep(1.1)
    assert (await ratelimit.take(key, per_minute=60, burst=3)).allowed
