import json

import httpx
import pytest

from app.providers.base import Grounding, ProviderError, ToolSpec
from app.providers.openai_compat import CohereEmbedder, OllamaEmbedder, OpenAICompatLLM

TOOLS = [ToolSpec("get_leave_balance", "leave", {"type": "object", "properties": {}})]


def llm_with(handler):
    return OpenAICompatLLM("groq", "https://api.groq.test/openai/v1", "openai/gpt-oss-20b", "gsk_test", transport=httpx.MockTransport(handler))


async def test_generate_sends_grounded_prompt_and_reads_usage():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "24 days [1]"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 120, "completion_tokens": 6}})

    r = await llm_with(handler).generate("SYS", [{"role": "user", "content": "q"}], Grounding("[1] Leave\n24 days", "How many days?"))
    body = seen["body"]
    assert seen["auth"] == "Bearer gsk_test"
    assert body["model"] == "openai/gpt-oss-20b" and body["temperature"] == 0.2 and body["max_tokens"] == 700
    assert body["messages"][0] == {"role": "system", "content": "SYS"}
    assert "<policy_context>" in body["messages"][-1]["content"] and "How many days?" in body["messages"][-1]["content"]
    assert r.text == "24 days [1]" and r.usage.input_tokens == 120 and r.usage.output_tokens == 6


async def test_retries_429_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": {"message": "rate limit"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})

    r = await llm_with(handler).generate("s", [{"role": "user", "content": "q"}])
    assert r.text == "ok" and calls["n"] == 2


async def test_auth_error_is_not_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401, json={"error": {"message": "Invalid API Key"}})

    with pytest.raises(ProviderError) as e:
        await llm_with(handler).generate("s", [{"role": "user", "content": "q"}])
    assert not e.value.retryable and calls["n"] == 1


async def test_tool_calls_round_trip():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "get_leave_balance", "arguments": "{\"leave_type\": \"all\"}"}}]}}], "usage": {"prompt_tokens": 50, "completion_tokens": 10}})

    history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c0", "name": "search_policies", "args": {"query": "x"}}]},
        {"role": "tool", "tool_call_id": "c0", "name": "search_policies", "content": "{}"},
        {"role": "user", "content": "leave left?"},
    ]
    r = await llm_with(handler).chat_with_tools("s", history, TOOLS)
    msgs = seen["body"]["messages"]
    assert msgs[2]["tool_calls"][0]["function"]["arguments"] == "{\"query\": \"x\"}"
    assert msgs[3] == {"role": "tool", "tool_call_id": "c0", "content": "{}"}
    assert seen["body"]["tools"][0]["function"]["name"] == "get_leave_balance"
    assert r.tool_calls[0].name == "get_leave_balance" and r.tool_calls[0].args == {"leave_type": "all"}


async def test_stream_parses_tokens_and_usage():
    chunks = [
        {"choices": [{"delta": {"content": "Hello "}}]},
        {"choices": [{"delta": {"content": "world"}}]},
        {"choices": [], "x_groq": {"usage": {"prompt_tokens": 30, "completion_tokens": 2}}},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"

    def handler(request):
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    events = [e async for e in llm_with(handler).stream("s", [{"role": "user", "content": "q"}])]
    assert "".join(e.text for e in events if e.type == "token") == "Hello world"
    assert events[-1].type == "done" and events[-1].usage.input_tokens == 30


async def test_ollama_embedder_checks_dimension():
    def handler(request):
        return httpx.Response(200, json={"embeddings": [[0.1] * 768]})

    e = OllamaEmbedder("http://ollama.test", "nomic-embed-text", 1024, transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError):
        await e.embed(["x"])


async def test_cohere_uses_query_input_type():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"embeddings": {"float": [[0.0] * 1024]}, "meta": {"billed_units": {"input_tokens": 3}}})

    e = CohereEmbedder("key", "embed-english-v3.0", 1024, transport=httpx.MockTransport(handler))
    vecs, tokens = await e.embed(["hello"], kind="query")
    assert seen["body"]["input_type"] == "search_query" and tokens == 3 and len(vecs[0]) == 1024


async def test_minute_limiter_blocks_over_limit(monkeypatch):
    import asyncio

    from app.providers import retry

    clock = {"t": 1000.0}
    slept = []
    monkeypatch.setattr(retry.time, "monotonic", lambda: clock["t"])

    async def fake_sleep(s):
        slept.append(s)
        clock["t"] += s

    monkeypatch.setattr(retry.asyncio, "sleep", fake_sleep)
    lim = retry.MinuteLimiter(2)
    for _ in range(3):
        await lim.acquire()
    assert len(slept) == 1 and 59 < slept[0] <= 60.1


async def test_429_waits_longer_then_succeeds(monkeypatch):
    from app.providers import retry

    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(retry.asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProviderError("rate", retryable=True, status=429)
        return "ok"

    assert await retry.with_retries(fn, attempts=2) == "ok"
    assert slept[0] >= 6
