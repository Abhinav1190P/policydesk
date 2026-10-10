import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.agent import state as store
from app.agent.runner import get_agent
from app.providers.base import ProviderError
from app.routes.deps import ai_rate_limit
from app.security import CurrentUser, current_user
from app.services.chat import ChatError

router = APIRouter(prefix="/api/agent", tags=["agent"])
SESSION_PATTERN = r"^[A-Za-z0-9-]{8,64}$"


class AgentIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(default=None, pattern=SESSION_PATTERN)


class ConfirmIn(BaseModel):
    session_id: str = Field(pattern=SESSION_PATTERN)
    action_id: str = Field(min_length=8, max_length=64)
    approve: bool


class ResetIn(BaseModel):
    session_id: str = Field(pattern=SESSION_PATTERN)


@router.post("/message")
async def message(body: AgentIn, user: CurrentUser = Depends(ai_rate_limit)):
    sid = body.session_id or str(uuid.uuid4())
    try:
        return await (await get_agent()).message(user, sid, body.text)
    except ChatError as e:
        raise HTTPException(e.status, str(e))
    except ProviderError as e:
        raise HTTPException(502, f"AI backend error: {e}")


@router.post("/confirm")
async def confirm(body: ConfirmIn, user: CurrentUser = Depends(ai_rate_limit)):
    try:
        return await (await get_agent()).confirm(user, body.session_id, body.action_id, body.approve)
    except ChatError as e:
        raise HTTPException(e.status, str(e))
    except ProviderError as e:
        raise HTTPException(502, f"AI backend error: {e}")


@router.post("/reset", status_code=204)
async def reset(body: ResetIn, user: CurrentUser = Depends(current_user)):
    await store.clear(user.id, body.session_id)
