import json
import uuid

from fastapi import APIRouter, HTTPException, Request

from app.agent.tools import leave_balance_for
from app.db.pool import get_pool
from app.security import verify_internal

router = APIRouter(prefix="/internal/tools", tags=["internal"])


@router.post("/leave-balance")
async def leave_balance(request: Request):
    body = await request.body()
    verify_internal(body, request.headers.get("x-pd-timestamp"), request.headers.get("x-pd-signature"))
    try:
        data = json.loads(body)
        tenant_id = str(uuid.UUID(data["tenant_id"]))
        user_id = str(uuid.UUID(data["user_id"]))
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "tenant_id and user_id are required")
    owner = await get_pool().fetchval("SELECT tenant_id FROM users WHERE id = $1", uuid.UUID(user_id))
    if owner is None or str(owner) != tenant_id:
        raise HTTPException(404, "Employee not found")
    return await leave_balance_for(tenant_id, user_id)
