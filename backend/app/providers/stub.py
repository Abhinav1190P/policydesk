import asyncio
import hashlib
import json
import math
import re
import uuid
from typing import AsyncIterator

from app.config import get_settings
from app.providers.base import GenResult, Grounding, StreamEvent, ToolCall, ToolSpec, Usage, estimate_tokens
from app.providers.text import sentences, terms
from app.rag.prompts import NOT_FOUND

_source_header = re.compile(r"^\[(\d+)\][^\n]*\n", re.M)

LEAVE_WORDS = re.compile(r"\b(leave|leaves|pto|vacation|holiday|sick|days? off)\b.*\b(left|balance|remaining|have|many)\b|\b(my|remaining)\b.*\b(leave|pto|balance)\b", re.I)
TICKET_WORDS = re.compile(r"\b(ticket|broken|not working|doesn't work|does not work|crash|crashing|can't log ?in|cannot log ?in|locked out|need access|request access|reset my|replace|vpn (is )?down|raise)\b", re.I)


def _parse_sources(context: str) -> list[tuple[int, str]]:
    out = []
    marks = list(_source_header.finditer(context))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(context)
        out.append((int(m.group(1)), context[m.end():end].strip()))
    return out


def extractive_answer(context: str, question: str) -> str:
    q = set(terms(question))
    if not q:
        return NOT_FOUND
    need = 1 if len(q) <= 2 else max(2, math.ceil(0.4 * len(q)))
    scored = []
    for n, body in _parse_sources(context):
        for s in sentences(body):
            if len(s.split()) < 6:
                continue
            st = set(terms(s))
            overlap = len(q & st)
            if overlap >= need:
                scored.append((overlap / len(q) + 0.01 * min(len(st), 25) / 25, n, s))
    scored.sort(key=lambda x: -x[0])
    picked = scored[:2]
    if not picked:
        return NOT_FOUND
    seen = set()
    parts = []
    for _, n, s in picked:
        if s in seen:
            continue
        seen.add(s)
        parts.append(f"{s.rstrip('.')}. [{n}]")
    return " ".join(parts)


def _guess_category(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ("laptop", "monitor", "keyboard", "mouse", "screen", "battery", "charger", "hardware")):
        return "hardware"
    if any(w in t for w in ("vpn", "wifi", "wi-fi", "network", "internet")):
        return "network"
    if any(w in t for w in ("access", "permission", "locked", "password", "login", "log in", "account")):
        return "access"
    if any(w in t for w in ("app", "software", "install", "crash", "outlook", "slack", "license")):
        return "software"
    return "other"


def _guess_priority(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ("urgent", "asap", "can't work", "cannot work", "blocked", "down", "locked out")):
        return "high"
    if any(w in t for w in ("whenever", "low priority", "no rush")):
        return "low"
    return "medium"


class StubLLM:
    backend = "stub"
    model = "stub-extractive-v1"

    async def _delay(self) -> None:
        ms = get_settings().stub_latency_ms
        if ms:
            await asyncio.sleep(ms / 1000)

    async def generate(self, system: str, messages: list[dict], grounding: Grounding | None = None) -> GenResult:
        await self._delay()
        prompt = system + "".join(str(m.get("content", "")) for m in messages)
        if grounding is not None:
            text = extractive_answer(grounding.context, grounding.question)
            prompt += grounding.context
        else:
            text = "I'm PolicyDesk. Ask me about your company's HR and IT policies."
        return GenResult(text=text, usage=Usage(estimate_tokens(prompt), estimate_tokens(text)))

    async def stream(self, system: str, messages: list[dict], grounding: Grounding | None = None) -> AsyncIterator[StreamEvent]:
        result = await self.generate(system, messages, grounding)
        words = result.text.split(" ")
        for i, w in enumerate(words):
            yield StreamEvent("token", w + (" " if i < len(words) - 1 else ""))
        yield StreamEvent("done", usage=result.usage)

    async def chat_with_tools(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> GenResult:
        await self._delay()
        names = {t.name for t in tools}
        last = messages[-1]
        prompt_tokens = estimate_tokens(system + json.dumps(messages))
        user_text = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        if last["role"] == "tool":
            payload = _safe_json(last["content"])
            text = self._summarize_tool(last.get("name", ""), payload, user_text)
            return GenResult(text=text, usage=Usage(prompt_tokens, estimate_tokens(text)))
        call = None
        if "get_leave_balance" in names and LEAVE_WORDS.search(user_text):
            call = ToolCall(f"call_{uuid.uuid4().hex[:8]}", "get_leave_balance", {"leave_type": "all"})
        elif "create_it_ticket" in names and TICKET_WORDS.search(user_text):
            call = ToolCall(
                f"call_{uuid.uuid4().hex[:8]}",
                "create_it_ticket",
                {"category": _guess_category(user_text), "summary": user_text[:200], "priority": _guess_priority(user_text)},
            )
        elif "search_policies" in names:
            call = ToolCall(f"call_{uuid.uuid4().hex[:8]}", "search_policies", {"query": user_text})
        if call is None:
            text = "I can help with policy questions, your leave balance, and IT tickets."
            return GenResult(text=text, usage=Usage(prompt_tokens, estimate_tokens(text)))
        return GenResult(text="", tool_calls=[call], stop_reason="tool_use", usage=Usage(prompt_tokens, 20))

    def _summarize_tool(self, name: str, payload: dict, user_text: str) -> str:
        if payload.get("error"):
            return f"Sorry, that didn't work: {payload['error']}"
        if name == "get_leave_balance":
            return (
                f"You have {payload.get('annual_remaining')} annual leave days and "
                f"{payload.get('sick_remaining')} sick days remaining this year."
            )
        if name == "create_it_ticket":
            if payload.get("status") == "cancelled":
                return "Okay, I didn't raise the ticket."
            return f"Done. Ticket #{payload.get('ticket_id')} is open with IT ({payload.get('category')}, {payload.get('priority')} priority)."
        if name == "search_policies":
            return extractive_answer(payload.get("context", ""), user_text)
        return json.dumps(payload)


def _safe_json(s: str) -> dict:
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {"result": v}
    except (TypeError, ValueError):
        return {"result": s}


class StubEmbedder:
    backend = "stub"
    model = "stub-hash-1024"

    def __init__(self, dim: int = 1024):
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        toks = terms(text)
        feats = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]
        for f in feats:
            h = hashlib.blake2b(f.encode(), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "little") % self.dim
            sign = 1.0 if h[4] & 1 else -1.0
            v[idx] += sign * (1.0 if "_" not in f else 0.5)
        norm = math.sqrt(sum(x * x for x in v))
        if norm == 0:
            v[0] = 1.0
            return v
        return [x / norm for x in v]

    async def embed(self, texts: list[str], kind: str = "document") -> tuple[list[list[float]], int]:
        ms = get_settings().stub_embed_latency_ms
        if ms:
            await asyncio.sleep(ms / 1000)
        return [self._vec(t) for t in texts], sum(estimate_tokens(t) for t in texts)
