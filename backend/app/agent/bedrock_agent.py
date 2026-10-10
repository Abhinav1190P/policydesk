import asyncio
import json
import time
import uuid

from botocore.exceptions import BotoCoreError, ClientError

from app.agent import state as store
from app.agent.tools import ToolArgError, insert_ticket, validate_ticket_args
from app.config import get_settings
from app.providers import bedrock
from app.security import CurrentUser
from app.infra import spend
from app.services import usage
from app.services.chat import ChatError

CONFIRM_FUNCTIONS = {"create_it_ticket"}


def agent_session_id(user_id: str, session_id: str) -> str:
    return f"{user_id.replace('-', '')}-{session_id.replace('-', '')}"[:100]


def session_attributes(user: CurrentUser) -> dict:
    return {"tenant_id": user.tenant_id, "user_id": user.id, "tenant_slug": user.tenant_slug}


def kb_configs(tenant_id: str) -> list[dict]:
    s = get_settings()
    if not s.bedrock_kb_id:
        return []
    return [
        {
            "knowledgeBaseId": s.bedrock_kb_id,
            "retrievalConfiguration": {
                "vectorSearchConfiguration": {
                    "numberOfResults": s.retrieval_k,
                    "filter": {"equals": {"key": "tenant_id", "value": tenant_id}},
                }
            },
        }
    ]


def build_invoke_request(user: CurrentUser, session_id: str, text: str | None = None, return_control: dict | None = None) -> dict:
    s = get_settings()
    state: dict = {"sessionAttributes": session_attributes(user)}
    kbs = kb_configs(user.tenant_id)
    if kbs:
        state["knowledgeBaseConfigurations"] = kbs
    if return_control:
        state["invocationId"] = return_control["invocation_id"]
        state["returnControlInvocationResults"] = [
            {
                "functionResult": {
                    "actionGroup": return_control["action_group"],
                    "function": return_control["function"],
                    "responseBody": {"TEXT": {"body": json.dumps(return_control["result"], default=str)}},
                }
            }
        ]
    req = {
        "agentId": s.bedrock_agent_id,
        "agentAliasId": s.bedrock_agent_alias_id,
        "sessionId": agent_session_id(user.id, session_id),
        "enableTrace": True,
        "sessionState": state,
    }
    if text:
        req["inputText"] = text
    return req


def parse_events(events) -> dict:
    out = {"text": "", "citations": [], "return_control": None, "trace": [], "input_tokens": 0, "output_tokens": 0}
    for ev in events:
        if "chunk" in ev:
            c = ev["chunk"]
            out["text"] += c.get("bytes", b"").decode("utf-8") if isinstance(c.get("bytes"), (bytes, bytearray)) else str(c.get("bytes", ""))
            for cit in (c.get("attribution") or {}).get("citations", []):
                for ref in cit.get("retrievedReferences", []):
                    meta = ref.get("metadata") or {}
                    out["citations"].append(
                        {
                            "n": len(out["citations"]) + 1,
                            "title": meta.get("title", "policy"),
                            "document_id": meta.get("doc_id"),
                            "snippet": (ref.get("content") or {}).get("text", "")[:280],
                            "source": "bedrock_kb",
                            "used": True,
                        }
                    )
        elif "returnControl" in ev:
            rc = ev["returnControl"]
            inv = rc["invocationInputs"][0]["functionInvocationInput"]
            params = {p["name"]: p.get("value") for p in inv.get("parameters", [])}
            out["return_control"] = {"invocation_id": rc["invocationId"], "action_group": inv["actionGroup"], "function": inv["function"], "args": params}
        elif "trace" in ev:
            orch = (ev["trace"].get("trace") or {}).get("orchestrationTrace") or {}
            if "invocationInput" in orch:
                ii = orch["invocationInput"]
                step = {"step": ii.get("invocationType", "").lower()}
                if "actionGroupInvocationInput" in ii:
                    step["name"] = ii["actionGroupInvocationInput"].get("function")
                if "knowledgeBaseLookupInput" in ii:
                    step["name"] = "knowledge_base"
                out["trace"].append(step)
            mo = orch.get("modelInvocationOutput") or {}
            u = (mo.get("metadata") or {}).get("usage") or {}
            out["input_tokens"] += u.get("inputTokens", 0)
            out["output_tokens"] += u.get("outputTokens", 0)
    return out


class BedrockAgent:
    mode = "bedrock_agent"

    async def _invoke(self, user: CurrentUser, req: dict) -> dict:
        reason = await spend.blocked_reason("bedrock_agent")
        if reason:
            raise ChatError("The Bedrock agent is paused (daily AWS spend cap or repeated errors). Try again later.", 429 if reason == "spend_cap" else 503)
        started = time.perf_counter()

        def run():
            resp = bedrock.client("bedrock-agent-runtime").invoke_agent(**req)
            return parse_events(resp["completion"])

        try:
            parsed = await asyncio.to_thread(run)
        except (ClientError, BotoCoreError) as e:
            await spend.record_failure("bedrock_agent")
            raise ChatError(f"Bedrock agent error: {bedrock.to_provider_error(e)}", 502)
        await spend.record_success("bedrock_agent")
        await usage.record(
            usage.usage_event(user.tenant_id, user.id, "agent", "bedrock_agent", get_settings().bedrock_model_id, parsed["input_tokens"], parsed["output_tokens"], int((time.perf_counter() - started) * 1000))
        )
        return parsed

    async def _respond(self, user: CurrentUser, session_id: str, parsed: dict, executed: dict | None = None) -> dict:
        rc = parsed["return_control"]
        if rc and rc["function"] in CONFIRM_FUNCTIONS:
            try:
                clean = validate_ticket_args(rc["args"])
            except ToolArgError as e:
                clean = None
                err = str(e)
            if clean is None:
                return await self._resume(user, session_id, {**rc, "result": {"error": err}})
            pending = {"id": uuid.uuid4().hex, **rc, "args": clean}
            st = await store.load(user.id, session_id)
            st["pending"] = pending
            await store.save(user.id, session_id, st)
            return {
                "type": "confirm",
                "session_id": session_id,
                "text": parsed["text"] or "Please confirm this action.",
                "action": {"id": pending["id"], "tool": rc["function"], "args": clean},
                "citations": parsed["citations"],
                "trace": parsed["trace"],
                "backend": "bedrock_agent",
                "fallback": None,
            }
        return {
            "type": "answer",
            "session_id": session_id,
            "text": parsed["text"].strip() or "Done.",
            "citations": parsed["citations"],
            "trace": parsed["trace"],
            "backend": "bedrock_agent",
            "fallback": None,
            "executed": executed,
        }

    async def _resume(self, user: CurrentUser, session_id: str, rc: dict) -> dict:
        parsed = await self._invoke(user, build_invoke_request(user, session_id, return_control=rc))
        return await self._respond(user, session_id, parsed, executed=rc["result"])

    async def message(self, user: CurrentUser, session_id: str, text: str) -> dict:
        text = text.strip()
        if not text:
            raise ChatError("Message is empty")
        st = await store.load(user.id, session_id)
        if st.get("pending"):
            st["pending"] = None
            await store.save(user.id, session_id, st)
        parsed = await self._invoke(user, build_invoke_request(user, session_id, text=text))
        return await self._respond(user, session_id, parsed)

    async def confirm(self, user: CurrentUser, session_id: str, action_id: str, approve: bool) -> dict:
        st = await store.load(user.id, session_id)
        p = st.get("pending")
        if not p or p["id"] != action_id:
            raise ChatError("No pending action with that id", 404)
        st["pending"] = None
        await store.save(user.id, session_id, st)
        if approve:
            result = await insert_ticket(user.tenant_id, user.id, p["args"], p["id"], via="bedrock_agent")
        else:
            result = {"status": "cancelled"}
        return await self._resume(user, session_id, {**p, "result": result})
