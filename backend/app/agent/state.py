import json

from app.config import get_settings
from app.infra.redis_client import get_redis


def _key(user_id: str, session_id: str) -> str:
    return f"agent:{user_id}:{session_id}"


async def load(user_id: str, session_id: str) -> dict:
    raw = await get_redis().get(_key(user_id, session_id))
    if raw:
        return json.loads(raw)
    return {"messages": [], "pending": None, "citations": [], "trace": []}


async def save(user_id: str, session_id: str, state: dict) -> None:
    state["messages"] = state["messages"][-40:]
    state["citations"] = state["citations"][-30:]
    await get_redis().set(_key(user_id, session_id), json.dumps(state, default=str), ex=get_settings().agent_session_ttl_s)


async def clear(user_id: str, session_id: str) -> None:
    await get_redis().delete(_key(user_id, session_id))
