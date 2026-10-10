import logging
import time
import uuid

from app.agent import state as store
from app.agent.tools import TOOLS, ToolArgError, ToolContext, tool_result, validate_ticket_args
from app.config import get_settings
from app.db.pool import get_pool
from app.providers.base import ProviderError
from app.providers.factory import fallback_llm, get_llm
from app.rag.prompts import AGENT_SYSTEM, normalize_citations
from app.security import CurrentUser
from app.infra import spend
from app.services import usage
from app.services.chat import ChatError, pick_llm

log = logging.getLogger("policydesk.agent")
MAX_STEPS = 5


async def tenant_name(tenant_id: str) -> str:
    return await get_pool().fetchval("SELECT name FROM tenants WHERE id = $1", uuid.UUID(tenant_id)) or "your company"


class LocalAgent:
    mode = "local"

    async def message(self, user: CurrentUser, session_id: str, text: str) -> dict:
        text = text.strip()
        if not text:
            raise ChatError("Message is empty")
        st = await store.load(user.id, session_id)
        if st.get("pending"):
            p = st["pending"]
            st["messages"].append({"role": "tool", "tool_call_id": p["tool_call_id"], "name": p["name"], "content": tool_result({"status": "cancelled", "reason": "user moved on"})})
            st["pending"] = None
        st["messages"].append({"role": "user", "content": text})
        st["trace"] = []
        return await self._loop(user, session_id, st)

    async def confirm(self, user: CurrentUser, session_id: str, action_id: str, approve: bool) -> dict:
        st = await store.load(user.id, session_id)
        p = st.get("pending")
        if not p or p["id"] != action_id:
            raise ChatError("No pending action with that id", 404)
        ctx = ToolContext(user.tenant_id, user.id, st["citations"])
        if approve:
            try:
                result = await TOOLS[p["name"]].run(ctx, p["args"], p["id"])
            except ToolArgError as e:
                result = {"error": str(e)}
        else:
            result = {"status": "cancelled"}
        st["messages"].append({"role": "tool", "tool_call_id": p["tool_call_id"], "name": p["name"], "content": tool_result(result)})
        st["pending"] = None
        st["trace"] = [{"step": "tool", "name": p["name"], "approved": approve}]
        return await self._loop(user, session_id, st, executed=result)

    async def _call(self, llm, system, messages, tools):
        try:
            res = await llm.chat_with_tools(system, messages, tools)
            await spend.record_success(llm.backend)
            return llm, res
        except ProviderError as e:
            await spend.record_failure(llm.backend)
            fb = fallback_llm()
            if fb is None or fb is llm:
                raise ChatError(f"AI backend unavailable: {e}", 502)
            log.warning("agent falling back to %s: %s", fb.backend, e)
            return fb, await fb.chat_with_tools(system, messages, tools)

    async def _loop(self, user: CurrentUser, session_id: str, st: dict, executed: dict | None = None) -> dict:
        llm, fallback_reason = await pick_llm(user.tenant_id)
        system = AGENT_SYSTEM.format(tenant_name=await tenant_name(user.tenant_id))
        tools = [t.spec for t in TOOLS.values()]
        ctx = ToolContext(user.tenant_id, user.id, st["citations"])
        cite_start = len(st["citations"])
        for _ in range(MAX_STEPS):
            if await spend.blocked_reason(llm.backend):
                llm, fallback_reason = await pick_llm(user.tenant_id)
            started = time.perf_counter()
            used, res = await self._call(llm, system, st["messages"], tools)
            await usage.record(usage.usage_event(user.tenant_id, user.id, "agent", used.backend, used.model, res.usage.input_tokens, res.usage.output_tokens, int((time.perf_counter() - started) * 1000)))
            if not res.tool_calls:
                answer = normalize_citations(res.text.strip()) or "Done."
                st["messages"].append({"role": "assistant", "content": answer})
                await store.save(user.id, session_id, st)
                return {
                    "type": "answer",
                    "session_id": session_id,
                    "text": answer,
                    "citations": st["citations"][cite_start:],
                    "trace": st["trace"],
                    "backend": used.backend,
                    "fallback": fallback_reason,
                    "executed": executed,
                }
            st["messages"].append(
                {"role": "assistant", "content": res.text, "tool_calls": [{"id": c.id, "name": c.name, "args": c.args} for c in res.tool_calls]}
            )
            pending = None
            for call in res.tool_calls:
                tool = TOOLS.get(call.name)
                if tool is None:
                    st["messages"].append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": tool_result({"error": "unknown tool"})})
                    continue
                if tool.needs_confirmation:
                    if pending is not None:
                        st["messages"].append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": tool_result({"error": "one action at a time"})})
                        continue
                    try:
                        clean = validate_ticket_args(call.args)
                    except ToolArgError as e:
                        st["messages"].append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": tool_result({"error": str(e)})})
                        continue
                    pending = {"id": uuid.uuid4().hex, "tool_call_id": call.id, "name": call.name, "args": clean}
                    st["trace"].append({"step": "confirm", "name": call.name, "args": clean})
                    continue
                try:
                    result = await tool.run(ctx, call.args, None)
                except ToolArgError as e:
                    result = {"error": str(e)}
                st["trace"].append({"step": "tool", "name": call.name, "args": call.args})
                st["messages"].append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": tool_result(result)})
            if pending is not None:
                st["pending"] = pending
                await store.save(user.id, session_id, st)
                return {
                    "type": "confirm",
                    "session_id": session_id,
                    "text": res.text or "Please confirm this action.",
                    "action": {"id": pending["id"], "tool": pending["name"], "args": pending["args"]},
                    "citations": st["citations"][cite_start:],
                    "trace": st["trace"],
                    "backend": used.backend,
                    "fallback": fallback_reason,
                }
        await store.save(user.id, session_id, st)
        return {"type": "answer", "session_id": session_id, "text": "I couldn't finish that. Please try rephrasing.", "citations": [], "trace": st["trace"], "backend": llm.backend, "fallback": fallback_reason, "executed": executed}


async def get_agent():
    if get_settings().agent_mode == "bedrock_agent":
        if await spend.blocked_reason("bedrock_agent") is None:
            from app.agent.bedrock_agent import BedrockAgent

            return BedrockAgent()
        log.warning("bedrock agent paused (spend cap or failures); using local agent with fallback model")
    return LocalAgent()
