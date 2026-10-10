import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.agent.tools import leave_balance_for
from app.db.pool import get_pool
from app.security import CurrentUser, admin_user, current_user

router = APIRouter(prefix="/api", tags=["tickets"])


class TicketPatch(BaseModel):
    status: Literal["open", "in_progress", "closed"]


@router.get("/tickets")
async def list_tickets(scope: Literal["mine", "tenant"] = "mine", user: CurrentUser = Depends(current_user)):
    pool = get_pool()
    if scope == "tenant":
        if user.role != "admin":
            raise HTTPException(403, "Admin only")
        rows = await pool.fetch(
            """
            SELECT t.id, t.category, t.summary, t.priority, t.status, t.created_via, t.created_at, u.name AS user_name
            FROM tickets t JOIN users u ON u.id = t.user_id
            WHERE t.tenant_id = $1 ORDER BY t.created_at DESC LIMIT 100
            """,
            uuid.UUID(user.tenant_id),
        )
    else:
        rows = await pool.fetch(
            "SELECT id, category, summary, priority, status, created_via, created_at FROM tickets WHERE user_id = $1 ORDER BY created_at DESC LIMIT 50",
            uuid.UUID(user.id),
        )
    return [dict(r) for r in rows]


@router.patch("/tickets/{ticket_id}")
async def update_ticket(ticket_id: int, body: TicketPatch, user: CurrentUser = Depends(admin_user)):
    row = await get_pool().fetchrow(
        "UPDATE tickets SET status = $3 WHERE id = $1 AND tenant_id = $2 RETURNING id, status",
        ticket_id, uuid.UUID(user.tenant_id), body.status,
    )
    if row is None:
        raise HTTPException(404, "Ticket not found")
    return dict(row)


@router.get("/me/leave")
async def my_leave(user: CurrentUser = Depends(current_user)):
    return await leave_balance_for(user.tenant_id, user.id)
