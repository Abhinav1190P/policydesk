import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.db.pool import get_pool
from app.providers.base import ProviderError
from app.routes.deps import ai_rate_limit
from app.security import CurrentUser, current_user
from app.services.chat import ChatError, run_chat

log = logging.getLogger("policydesk.routes.chat")
router = APIRouter(prefix="/api", tags=["chat"])


class ChatIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None


def _sse(ev: dict) -> str:
    return f"data: {json.dumps(ev, default=str)}\n\n"


@router.post("/chat/stream")
async def chat_stream(body: ChatIn, user: CurrentUser = Depends(ai_rate_limit)):
    async def gen():
        try:
            async for ev in run_chat(user, body.question, body.conversation_id):
                yield _sse(ev)
        except ChatError as e:
            yield _sse({"type": "error", "message": str(e), "status": e.status})
        except ProviderError as e:
            log.exception("provider failure")
            yield _sse({"type": "error", "message": f"AI backend error: {e}", "status": 502})
        except Exception:
            log.exception("chat stream failed")
            yield _sse({"type": "error", "message": "Something went wrong", "status": 500})

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/chat")
async def chat(body: ChatIn, user: CurrentUser = Depends(ai_rate_limit)):
    meta: dict = {}
    citations: list = []
    try:
        async for ev in run_chat(user, body.question, body.conversation_id):
            if ev["type"] == "meta":
                meta = ev
            elif ev["type"] == "citations":
                citations = ev["citations"]
            elif ev["type"] == "done":
                return {
                    "conversation_id": meta.get("conversation_id"),
                    "backend": meta.get("backend"),
                    "model": meta.get("model"),
                    "fallback": meta.get("fallback"),
                    "instance": meta.get("instance"),
                    "answer": ev["answer"],
                    "citations": ev.get("citations", citations),
                    "cached": ev["cached"],
                    "latency_ms": ev["latency_ms"],
                    "usage": ev["usage"],
                    "guardrail": ev["guardrail"],
                    "message_id": ev["message_id"],
                }
    except ChatError as e:
        raise HTTPException(e.status, str(e))
    raise HTTPException(500, "No answer produced")


@router.get("/conversations")
async def conversations(user: CurrentUser = Depends(current_user)):
    rows = await get_pool().fetch(
        "SELECT id, title, updated_at FROM conversations WHERE user_id = $1 ORDER BY updated_at DESC LIMIT 50",
        uuid.UUID(user.id),
    )
    return [{"id": str(r["id"]), "title": r["title"], "updated_at": r["updated_at"]} for r in rows]


@router.get("/conversations/{conversation_id}/messages")
async def conversation_messages(conversation_id: uuid.UUID, user: CurrentUser = Depends(current_user)):
    pool = get_pool()
    owner = await pool.fetchval("SELECT user_id FROM conversations WHERE id = $1", conversation_id)
    if owner is None or str(owner) != user.id:
        raise HTTPException(404, "Conversation not found")
    rows = await pool.fetch(
        "SELECT id, role, content, citations, backend, cached, latency_ms, guardrail, created_at FROM messages WHERE conversation_id = $1 ORDER BY id",
        conversation_id,
    )
    return [{**dict(r), "citations": json.loads(r["citations"]) if isinstance(r["citations"], str) else r["citations"]} for r in rows]


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: uuid.UUID, user: CurrentUser = Depends(current_user)):
    res = await get_pool().execute("DELETE FROM conversations WHERE id = $1 AND user_id = $2", conversation_id, uuid.UUID(user.id))
    if res.endswith("0"):
        raise HTTPException(404, "Conversation not found")
