import json
import logging
import re
import time
import uuid
from typing import AsyncIterator

from app.config import get_settings
from app.db.pool import get_pool
from app.infra import cache, spend
from app.providers.base import Grounding, ProviderError
from app.providers.factory import fallback_llm, get_embedder, get_llm
from app.rag.prompts import NOT_FOUND, RAG_SYSTEM, build_context, normalize_citations
from app.rag.retriever import get_retriever
from app.security import CurrentUser
from app.services import guard, usage

log = logging.getLogger("policydesk.chat")
_cite = re.compile(r"\[(\d+)\]")


class ChatError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


async def ensure_conversation(user: CurrentUser, conversation_id: str | None, title: str) -> str:
    pool = get_pool()
    if conversation_id:
        row = await pool.fetchrow(
            "SELECT id FROM conversations WHERE id = $1 AND user_id = $2 AND tenant_id = $3",
            uuid.UUID(conversation_id), uuid.UUID(user.id), uuid.UUID(user.tenant_id),
        )
        if row is None:
            raise ChatError("Conversation not found", 404)
        return conversation_id
    cid = await pool.fetchval(
        "INSERT INTO conversations (tenant_id, user_id, title) VALUES ($1, $2, $3) RETURNING id",
        uuid.UUID(user.tenant_id), uuid.UUID(user.id), title[:80],
    )
    return str(cid)


async def load_history(conversation_id: str, turns: int) -> list[dict]:
    rows = await get_pool().fetch(
        "SELECT role, content FROM messages WHERE conversation_id = $1 ORDER BY id DESC LIMIT $2",
        uuid.UUID(conversation_id), turns * 2,
    )
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


async def save_turn(conversation_id: str, question: str, answer: str, citations: list, backend: str, model: str, in_tok: int, out_tok: int, latency_ms: int, cached: bool, guardrail: str | None) -> int:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES ($1, 'user', $2)",
                uuid.UUID(conversation_id), question,
            )
            mid = await conn.fetchval(
                """
                INSERT INTO messages (conversation_id, role, content, citations, backend, model, input_tokens, output_tokens, latency_ms, cached, guardrail)
                VALUES ($1, 'assistant', $2, $3::jsonb, $4, $5, $6, $7, $8, $9, $10) RETURNING id
                """,
                uuid.UUID(conversation_id), answer, json.dumps(citations), backend, model, in_tok, out_tok, latency_ms, cached, guardrail,
            )
            await conn.execute("UPDATE conversations SET updated_at = now() WHERE id = $1", uuid.UUID(conversation_id))
    return mid


def used_citations(answer: str, hits: list) -> list[dict]:
    if answer.strip() == NOT_FOUND:
        return []
    refs = {int(n) for n in _cite.findall(answer)}
    cites = [h.citation(i) for i, h in enumerate(hits, start=1)]
    for c in cites:
        c["used"] = c["n"] in refs
    return cites


async def pick_llm(tenant_id: str):
    primary = get_llm()
    over = await cache.budget_exhausted(tenant_id) or await cache.global_budget_exhausted()
    reason = "budget" if over else await spend.blocked_reason(primary.backend)
    if reason:
        fb = fallback_llm()
        if fb is None or fb is primary:
            msg = {
                "budget": "Daily AI budget reached. Try again tomorrow.",
                "spend_cap": "The daily AWS spending cap has been reached. Try again tomorrow.",
                "circuit_open": "The AI service is failing right now and has been paused for a few minutes.",
                "aws_disabled": "AWS calls are switched off on this server.",
            }[reason]
            raise ChatError(msg, 429 if reason in ("budget", "spend_cap") else 503)
        return fb, reason
    return primary, None


async def run_chat(user: CurrentUser, question: str, conversation_id: str | None = None) -> AsyncIterator[dict]:
    s = get_settings()
    question = question.strip()
    if not question:
        raise ChatError("Question is empty")
    if len(question) > guard.MAX_QUESTION_CHARS or len(question) // 4 > s.max_question_tokens:
        raise ChatError("Question is too long")
    started = time.perf_counter()
    conversation_id = await ensure_conversation(user, conversation_id, question)
    history = await load_history(conversation_id, s.history_turns)
    llm, fallback_reason = await pick_llm(user.tenant_id)
    yield {"type": "meta", "conversation_id": conversation_id, "backend": llm.backend, "model": llm.model, "fallback": fallback_reason, "instance": s.instance_id}

    cache_key = None
    if not history:
        cache_key = await cache.answer_key(user.tenant_id, question, llm.backend)
        hit = await cache.get_answer(cache_key)
        if hit:
            latency = int((time.perf_counter() - started) * 1000)
            yield {"type": "citations", "citations": hit["citations"]}
            yield {"type": "token", "text": hit["answer"]}
            mid = await save_turn(conversation_id, question, hit["answer"], hit["citations"], hit["backend"], hit["model"], 0, 0, latency, True, None)
            await usage.record(usage.usage_event(user.tenant_id, user.id, "chat", hit["backend"], hit["model"], 0, 0, latency, cached=True))
            yield {"type": "done", "answer": hit["answer"], "message_id": mid, "cached": True, "latency_ms": latency, "usage": {"input_tokens": 0, "output_tokens": 0}, "guardrail": None}
            return

    retriever = get_retriever()
    hits, embed_tokens = await retriever.retrieve(user.tenant_id, question)
    if embed_tokens:
        emb = get_embedder()
        await usage.record(usage.usage_event(user.tenant_id, user.id, "embed", emb.backend, emb.model, embed_tokens, 0, None))
    citations_all = [h.citation(i) for i, h in enumerate(hits, start=1)]
    yield {"type": "citations", "citations": citations_all}

    in_tok = out_tok = 0
    guardrail = None
    if not hits:
        answer = NOT_FOUND
        yield {"type": "token", "text": answer}
        backend, model = "none", "none"
    else:
        context = build_context(hits)
        grounding = Grounding(context, question)
        messages = history + [{"role": "user", "content": question}]
        parts: list[str] = []
        candidates = [llm] + ([fb] if (fb := fallback_llm()) and fb is not llm else [])
        last_err: Exception | None = None
        used = llm
        for candidate in candidates:
            try:
                async for ev in candidate.stream(RAG_SYSTEM, messages, grounding):
                    if ev.type == "token":
                        parts.append(ev.text)
                        yield {"type": "token", "text": ev.text}
                    elif ev.type == "done":
                        in_tok, out_tok = ev.usage.input_tokens, ev.usage.output_tokens
                        guardrail = ev.guardrail
                used = candidate
                last_err = None
                await spend.record_success(candidate.backend)
                break
            except ProviderError as e:
                last_err = e
                await spend.record_failure(candidate.backend)
                log.warning("backend %s failed: %s", candidate.backend, e)
                if parts:
                    break
                if candidate is not candidates[-1]:
                    yield {"type": "meta", "conversation_id": conversation_id, "backend": candidates[-1].backend, "model": candidates[-1].model, "fallback": "error", "instance": s.instance_id}
        if last_err is not None:
            raise ChatError(f"AI backend unavailable: {last_err}", 502)
        backend, model = used.backend, used.model
        answer = normalize_citations("".join(parts).strip())
        if backend != "bedrock" or not s.bedrock_guardrail_id:
            masked, changed = guard.mask_pii(answer, guard.allowed_contacts(context))
            if changed:
                answer = masked
                guardrail = guardrail or "pii_masked"

    citations = used_citations(answer, hits)
    latency = int((time.perf_counter() - started) * 1000)
    mid = await save_turn(conversation_id, question, answer, citations, backend, model, in_tok, out_tok, latency, False, guardrail)
    if backend != "none":
        await usage.record(usage.usage_event(user.tenant_id, user.id, "chat", backend, model, in_tok, out_tok, latency))
    if cache_key and guardrail is None:
        await cache.set_answer(cache_key, {"answer": answer, "citations": citations, "backend": backend, "model": model})
    yield {
        "type": "done",
        "answer": answer,
        "message_id": mid,
        "cached": False,
        "latency_ms": latency,
        "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
        "guardrail": guardrail,
        "citations": citations,
    }
