import asyncio
import json
import threading
from typing import Any, AsyncIterator, Iterator

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import get_settings
from app.providers.base import GenResult, Grounding, ProviderError, StreamEvent, ToolCall, ToolSpec, Usage
from app.rag.prompts import inline_grounding

RETRYABLE_CODES = {"ThrottlingException", "ServiceUnavailableException", "ModelNotReadyException", "InternalServerException", "ModelTimeoutException"}

_clients: dict[str, Any] = {}


def boto_config() -> Config:
    s = get_settings()
    return Config(
        region_name=s.aws_region,
        retries={"mode": "adaptive", "max_attempts": s.provider_max_retries + 1},
        read_timeout=int(s.request_timeout_s),
        connect_timeout=10,
    )


def client(service: str):
    if service not in _clients:
        _clients[service] = boto3.client(service, config=boto_config())
    return _clients[service]


def set_client(service: str, c) -> None:
    _clients[service] = c


def reset_clients() -> None:
    _clients.clear()


def to_provider_error(e: Exception) -> ProviderError:
    if isinstance(e, ClientError):
        code = e.response.get("Error", {}).get("Code", "")
        msg = e.response.get("Error", {}).get("Message", str(e))
        status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return ProviderError(f"bedrock {code}: {msg}", retryable=code in RETRYABLE_CODES, status=status)
    return ProviderError(f"bedrock error: {e}", retryable=True)


def to_bedrock_messages(messages: list[dict]) -> list[dict]:
    out: list[dict] = []

    def push(role: str, blocks: list[dict]) -> None:
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for m in messages:
        if m["role"] == "tool":
            push(
                "user",
                [{"toolResult": {"toolUseId": m["tool_call_id"], "content": [{"text": m["content"]}], "status": "error" if '"error"' in m["content"] else "success"}}],
            )
        elif m["role"] == "assistant":
            blocks = [{"text": m["content"]}] if m.get("content") else []
            for c in m.get("tool_calls") or []:
                blocks.append({"toolUse": {"toolUseId": c["id"], "name": c["name"], "input": c["args"]}})
            push("assistant", blocks or [{"text": " "}])
        elif isinstance(m.get("content"), list):
            push("user", m["content"])
        else:
            push("user", [{"text": m["content"]}])
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"text": "Hello"}]})
    return out


def guardrail_config(stream: bool = False) -> dict | None:
    s = get_settings()
    if not s.bedrock_guardrail_id:
        return None
    cfg = {"guardrailIdentifier": s.bedrock_guardrail_id, "guardrailVersion": s.bedrock_guardrail_version, "trace": "enabled"}
    if stream:
        cfg["streamProcessingMode"] = "sync"
    return cfg


def apply_grounding(messages: list[dict], grounding: Grounding | None, guarded: bool) -> list[dict]:
    if grounding is None:
        return messages
    if not guarded:
        return inline_grounding(messages, grounding)
    out = list(messages[:-1])
    out.append(
        {
            "role": "user",
            "content": [
                {"guardContent": {"text": {"text": grounding.context, "qualifiers": ["grounding_source"]}}},
                {"guardContent": {"text": {"text": grounding.question, "qualifiers": ["query"]}}},
                {"text": "Answer the question above using only the policy context, citing [n] markers."},
            ],
        }
    )
    return out


def build_converse_request(
    model_id: str,
    system: str,
    messages: list[dict],
    grounding: Grounding | None = None,
    tools: list[ToolSpec] | None = None,
    stream: bool = False,
) -> dict:
    s = get_settings()
    guard = guardrail_config(stream) if not tools else None
    req: dict = {
        "modelId": model_id,
        "system": [{"text": system}],
        "messages": to_bedrock_messages(apply_grounding(messages, grounding, guard is not None)),
        "inferenceConfig": {"maxTokens": s.max_tokens, "temperature": s.temperature, "topP": s.top_p},
    }
    if guard:
        req["guardrailConfig"] = guard
    if tools:
        req["toolConfig"] = {
            "tools": [{"toolSpec": {"name": t.name, "description": t.description, "inputSchema": {"json": t.parameters}}} for t in tools],
            "toolChoice": {"auto": {}},
        }
    return req


def parse_converse_response(resp: dict) -> GenResult:
    content = resp.get("output", {}).get("message", {}).get("content", [])
    text = "".join(b.get("text", "") for b in content)
    calls = [ToolCall(b["toolUse"]["toolUseId"], b["toolUse"]["name"], b["toolUse"].get("input") or {}) for b in content if "toolUse" in b]
    u = resp.get("usage", {})
    stop = resp.get("stopReason", "end_turn")
    return GenResult(
        text=text,
        tool_calls=calls,
        usage=Usage(u.get("inputTokens", 0), u.get("outputTokens", 0)),
        stop_reason=stop,
        guardrail="intervened" if stop == "guardrail_intervened" else None,
    )


async def iterate_in_thread(sync_iter_factory) -> AsyncIterator[Any]:
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    sentinel = object()

    def worker():
        try:
            for item in sync_iter_factory():
                asyncio.run_coroutine_threadsafe(q.put(item), loop).result()
        except Exception as e:
            asyncio.run_coroutine_threadsafe(q.put(e), loop).result()
        finally:
            asyncio.run_coroutine_threadsafe(q.put(sentinel), loop).result()

    threading.Thread(target=worker, daemon=True).start()
    while True:
        item = await q.get()
        if item is sentinel:
            return
        if isinstance(item, Exception):
            raise item
        yield item


class BedrockLLM:
    backend = "bedrock"

    def __init__(self, model_id: str | None = None):
        self.model = model_id or get_settings().bedrock_model_id

    async def _call(self, fn, **kwargs):
        from app.infra import spend

        if await spend.blocked_reason("bedrock"):
            raise ProviderError("Bedrock is paused (daily AWS spend cap or repeated errors)", retryable=False)
        try:
            return await asyncio.to_thread(fn, **kwargs)
        except (ClientError, BotoCoreError) as e:
            raise to_provider_error(e)

    async def generate(self, system: str, messages: list[dict], grounding: Grounding | None = None) -> GenResult:
        req = build_converse_request(self.model, system, messages, grounding)
        resp = await self._call(client("bedrock-runtime").converse, **req)
        return parse_converse_response(resp)

    async def stream(self, system: str, messages: list[dict], grounding: Grounding | None = None) -> AsyncIterator[StreamEvent]:
        req = build_converse_request(self.model, system, messages, grounding, stream=True)
        resp = await self._call(client("bedrock-runtime").converse_stream, **req)

        def events() -> Iterator[dict]:
            yield from resp["stream"]

        usage = Usage()
        guard = None
        try:
            async for ev in iterate_in_thread(events):
                if "contentBlockDelta" in ev:
                    piece = ev["contentBlockDelta"].get("delta", {}).get("text")
                    if piece:
                        yield StreamEvent("token", piece)
                elif "messageStop" in ev:
                    if ev["messageStop"].get("stopReason") == "guardrail_intervened":
                        guard = "intervened"
                elif "metadata" in ev:
                    u = ev["metadata"].get("usage", {})
                    usage = Usage(u.get("inputTokens", 0), u.get("outputTokens", 0))
        except (ClientError, BotoCoreError) as e:
            raise to_provider_error(e)
        yield StreamEvent("done", usage=usage, guardrail=guard)

    async def chat_with_tools(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> GenResult:
        req = build_converse_request(self.model, system, messages, tools=tools)
        resp = await self._call(client("bedrock-runtime").converse, **req)
        return parse_converse_response(resp)


class TitanEmbedder:
    backend = "bedrock"

    def __init__(self, model_id: str | None = None, dim: int = 1024, concurrency: int = 8):
        self.model = model_id or get_settings().bedrock_embed_model_id
        self.dim = dim
        self.sem = asyncio.Semaphore(concurrency)

    def build_request(self, text: str) -> dict:
        return {
            "modelId": self.model,
            "contentType": "application/json",
            "accept": "application/json",
            "body": json.dumps({"inputText": text[:40000], "dimensions": self.dim, "normalize": True}),
        }

    async def _one(self, text: str) -> tuple[list[float], int]:
        async with self.sem:
            try:
                resp = await asyncio.to_thread(client("bedrock-runtime").invoke_model, **self.build_request(text))
            except (ClientError, BotoCoreError) as e:
                raise to_provider_error(e)
            body = json.loads(resp["body"].read())
            return body["embedding"], body.get("inputTextTokenCount", 0)

    async def embed(self, texts: list[str], kind: str = "document") -> tuple[list[list[float]], int]:
        from app.infra import spend

        if await spend.blocked_reason("bedrock"):
            raise ProviderError("Bedrock is paused (daily AWS spend cap or repeated errors)", retryable=False)
        results = await asyncio.gather(*(self._one(t) for t in texts))
        return [r[0] for r in results], sum(r[1] for r in results)
