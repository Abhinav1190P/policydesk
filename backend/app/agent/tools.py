import json
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

from app.db.pool import get_pool
from app.providers.base import ToolSpec
from app.rag.retriever import get_retriever

CATEGORIES = ("hardware", "software", "access", "network", "other")
PRIORITIES = ("low", "medium", "high")


@dataclass
class ToolContext:
    tenant_id: str
    user_id: str
    citations: list


@dataclass
class Tool:
    spec: ToolSpec
    run: Callable[[ToolContext, dict, str | None], Awaitable[dict]]
    needs_confirmation: bool = False


class ToolArgError(ValueError):
    pass


async def search_policies(ctx: ToolContext, args: dict, _key: str | None = None) -> dict:
    query = str(args.get("query") or "").strip()[:500]
    if not query:
        raise ToolArgError("query is required")
    hits, _ = await get_retriever().retrieve(ctx.tenant_id, query)
    offset = len(ctx.citations)
    for i, h in enumerate(hits, start=offset + 1):
        ctx.citations.append(h.citation(i))
    if not hits:
        return {"context": "", "sources": [], "note": "No matching policy text found."}
    text = build_context_offset(hits, offset)
    return {"context": text, "sources": [{"n": offset + i, "title": h.title} for i, h in enumerate(hits, start=1)]}


def build_context_offset(hits, offset: int) -> str:
    parts = []
    for i, h in enumerate(hits, start=offset + 1):
        header = f"[{i}] {h.title}" + (f" - {h.heading}" if h.heading else "")
        parts.append(f"{header}\n{h.context or h.content}")
    return "\n\n".join(parts)


async def get_leave_balance(ctx: ToolContext, args: dict, _key: str | None = None) -> dict:
    return await leave_balance_for(ctx.tenant_id, ctx.user_id)


async def leave_balance_for(tenant_id: str, user_id: str) -> dict:
    row = await get_pool().fetchrow(
        "SELECT annual_total, annual_used, sick_total, sick_used FROM leave_balances WHERE user_id = $1 AND tenant_id = $2",
        uuid.UUID(user_id), uuid.UUID(tenant_id),
    )
    if row is None:
        return {"error": "No leave record found for this employee."}
    return {
        "annual_total": float(row["annual_total"]),
        "annual_used": float(row["annual_used"]),
        "annual_remaining": float(row["annual_total"] - row["annual_used"]),
        "sick_total": float(row["sick_total"]),
        "sick_used": float(row["sick_used"]),
        "sick_remaining": float(row["sick_total"] - row["sick_used"]),
    }


def validate_ticket_args(args: dict) -> dict:
    category = str(args.get("category", "other")).lower().strip()
    priority = str(args.get("priority", "medium")).lower().strip()
    summary = " ".join(str(args.get("summary", "")).split())[:300]
    if category not in CATEGORIES:
        category = "other"
    if priority not in PRIORITIES:
        priority = "medium"
    if len(summary) < 5:
        raise ToolArgError("summary must describe the problem")
    return {"category": category, "priority": priority, "summary": summary}


async def create_it_ticket(ctx: ToolContext, args: dict, idempotency_key: str | None = None) -> dict:
    a = validate_ticket_args(args)
    return await insert_ticket(ctx.tenant_id, ctx.user_id, a, idempotency_key)


async def insert_ticket(tenant_id: str, user_id: str, a: dict, idempotency_key: str | None, via: str = "agent") -> dict:
    pool = get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO tickets (tenant_id, user_id, category, summary, priority, created_via, idempotency_key)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id
        """,
        uuid.UUID(tenant_id), uuid.UUID(user_id), a["category"], a["summary"], a["priority"], via, idempotency_key,
    )
    if row is None:
        row = await pool.fetchrow("SELECT id FROM tickets WHERE idempotency_key = $1", idempotency_key)
    return {"ticket_id": row["id"], "status": "open", **a}


TOOLS: dict[str, Tool] = {
    "search_policies": Tool(
        ToolSpec(
            "search_policies",
            "Search the employee's company HR and IT policy documents. Returns numbered policy excerpts to cite.",
            {"type": "object", "properties": {"query": {"type": "string", "description": "What to look for"}}, "required": ["query"]},
        ),
        search_policies,
    ),
    "get_leave_balance": Tool(
        ToolSpec(
            "get_leave_balance",
            "Get the signed-in employee's remaining annual and sick leave for this year.",
            {"type": "object", "properties": {"leave_type": {"type": "string", "enum": ["annual", "sick", "all"]}}, "required": []},
        ),
        get_leave_balance,
    ),
    "create_it_ticket": Tool(
        ToolSpec(
            "create_it_ticket",
            "Raise an IT support ticket for the signed-in employee. The employee must confirm before it is created.",
            {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "summary": {"type": "string", "description": "One or two sentences describing the problem"},
                    "priority": {"type": "string", "enum": list(PRIORITIES)},
                },
                "required": ["category", "summary", "priority"],
            },
        ),
        create_it_ticket,
        needs_confirmation=True,
    ),
}


def tool_result(payload: dict) -> str:
    return json.dumps(payload, default=str)
