import json
from typing import AsyncIterator

import httpx

from app.config import get_settings
from app.providers.base import (
    GenResult,
    Grounding,
    ProviderError,
    StreamEvent,
    ToolCall,
    ToolSpec,
    Usage,
    estimate_tokens,
)
from app.providers.retry import MinuteLimiter, with_retries
from app.rag.prompts import inline_grounding


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    out = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            out.append(
                {
                    "role": "assistant",
                    "content": m.get("content") or None,
                    "tool_calls": [
                        {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                        for c in m["tool_calls"]
                    ],
                }
            )
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


def to_openai_tools(tools: list[ToolSpec]) -> list[dict]:
    return [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}} for t in tools]


class OpenAICompatLLM:
    def __init__(self, backend: str, base_url: str, model: str, api_key: str = "", transport: httpx.AsyncBaseTransport | None = None, per_minute: int = 0):
        s = get_settings()
        self.limiter = MinuteLimiter(per_minute)
        self.backend = backend
        self.model = model
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=s.request_timeout_s, transport=transport)

    def _params(self) -> dict:
        s = get_settings()
        return {"model": self.model, "temperature": s.temperature, "top_p": s.top_p, "max_tokens": s.max_tokens}

    def _raise_for(self, r: httpx.Response) -> None:
        if r.status_code < 400:
            return
        retryable = r.status_code == 429 or r.status_code >= 500
        try:
            detail = r.json().get("error", {}).get("message", r.text)
        except ValueError:
            detail = r.text
        raise ProviderError(f"{self.backend} {r.status_code}: {detail[:300]}", retryable=retryable, status=r.status_code)

    async def _post(self, body: dict) -> dict:
        async def call():
            await self.limiter.acquire()
            try:
                r = await self.client.post("/chat/completions", json=body)
            except httpx.TransportError as e:
                raise ProviderError(f"{self.backend} unreachable: {e}", retryable=True)
            self._raise_for(r)
            return r.json()

        return await with_retries(call, attempts=get_settings().provider_max_retries)

    async def generate(self, system: str, messages: list[dict], grounding: Grounding | None = None) -> GenResult:
        body = {**self._params(), "messages": to_openai_messages(system, inline_grounding(messages, grounding))}
        data = await self._post(body)
        choice = data["choices"][0]
        u = data.get("usage") or {}
        return GenResult(
            text=choice["message"].get("content") or "",
            usage=Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0)),
            stop_reason=choice.get("finish_reason", "stop"),
        )

    async def stream(self, system: str, messages: list[dict], grounding: Grounding | None = None) -> AsyncIterator[StreamEvent]:
        msgs = to_openai_messages(system, inline_grounding(messages, grounding))
        body = {**self._params(), "messages": msgs, "stream": True, "stream_options": {"include_usage": True}}
        usage = None
        produced = []
        await self.limiter.acquire()
        try:
            async with self.client.stream("POST", "/chat/completions", json=body) as r:
                if r.status_code >= 400:
                    await r.aread()
                    self._raise_for(r)
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    u = chunk.get("usage") or (chunk.get("x_groq") or {}).get("usage")
                    if u:
                        usage = Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
                    for c in chunk.get("choices", []):
                        piece = (c.get("delta") or {}).get("content")
                        if piece:
                            produced.append(piece)
                            yield StreamEvent("token", piece)
        except httpx.TransportError as e:
            raise ProviderError(f"{self.backend} stream failed: {e}", retryable=True)
        if usage is None:
            usage = Usage(estimate_tokens(json.dumps(msgs)), estimate_tokens("".join(produced)))
        yield StreamEvent("done", usage=usage)

    async def chat_with_tools(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> GenResult:
        body = {
            **self._params(),
            "messages": to_openai_messages(system, messages),
            "tools": to_openai_tools(tools),
            "tool_choice": "auto",
        }
        data = await self._post(body)
        msg = data["choices"][0]["message"]
        u = data.get("usage") or {}
        calls = []
        for c in msg.get("tool_calls") or []:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            calls.append(ToolCall(c["id"], c["function"]["name"], args))
        return GenResult(
            text=msg.get("content") or "",
            tool_calls=calls,
            stop_reason="tool_use" if calls else "end_turn",
            usage=Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0)),
        )

    async def aclose(self) -> None:
        await self.client.aclose()


class OllamaEmbedder:
    backend = "local"

    def __init__(self, base_url: str, model: str, dim: int, transport: httpx.AsyncBaseTransport | None = None):
        self.model = model
        self.dim = dim
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=get_settings().request_timeout_s, transport=transport)

    async def embed(self, texts: list[str], kind: str = "document") -> tuple[list[list[float]], int]:
        async def call():
            try:
                r = await self.client.post("/api/embed", json={"model": self.model, "input": texts})
            except httpx.TransportError as e:
                raise ProviderError(f"ollama unreachable: {e}", retryable=True)
            if r.status_code >= 400:
                raise ProviderError(f"ollama {r.status_code}: {r.text[:200]}", retryable=r.status_code >= 500)
            return r.json()

        data = await with_retries(call, attempts=get_settings().provider_max_retries)
        vecs = data["embeddings"]
        if vecs and len(vecs[0]) != self.dim:
            raise ProviderError(f"embedding dim {len(vecs[0])} != {self.dim}; use a {self.dim}-dim model such as mxbai-embed-large")
        return vecs, data.get("prompt_eval_count") or sum(estimate_tokens(t) for t in texts)


class CohereEmbedder:
    backend = "cohere"

    def __init__(self, api_key: str, model: str, dim: int, transport: httpx.AsyncBaseTransport | None = None, per_minute: int = 0):
        self.limiter = MinuteLimiter(per_minute)
        self.model = model
        self.dim = dim
        self.client = httpx.AsyncClient(
            base_url="https://api.cohere.com",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=get_settings().request_timeout_s,
            transport=transport,
        )

    async def embed(self, texts: list[str], kind: str = "document") -> tuple[list[list[float]], int]:
        input_type = "search_query" if kind == "query" else "search_document"
        out: list[list[float]] = []
        tokens = 0
        for i in range(0, len(texts), 96):
            batch = texts[i : i + 96]

            async def call(batch=batch):
                await self.limiter.acquire()
                try:
                    r = await self.client.post(
                        "/v2/embed",
                        json={"model": self.model, "texts": batch, "input_type": input_type, "embedding_types": ["float"]},
                    )
                except httpx.TransportError as e:
                    raise ProviderError(f"cohere unreachable: {e}", retryable=True)
                if r.status_code >= 400:
                    raise ProviderError(f"cohere {r.status_code}: {r.text[:200]}", retryable=r.status_code == 429 or r.status_code >= 500)
                return r.json()

            data = await with_retries(call, attempts=get_settings().provider_max_retries)
            out.extend(data["embeddings"]["float"])
            tokens += (data.get("meta") or {}).get("billed_units", {}).get("input_tokens", 0) or sum(estimate_tokens(t) for t in batch)
        return out, tokens
