import uuid

from tests.conftest import get_pool


def sid():
    return f"s-{uuid.uuid4().hex[:12]}"


async def say(client, who, text, session_id):
    r = await client.post("/api/agent/message", headers=who["headers"], json={"text": text, "session_id": session_id})
    assert r.status_code == 200, r.text
    return r.json()


async def test_leave_balance_uses_signed_in_identity(client, acme_employee, globex_employee):
    a = await say(client, acme_employee, "How many leave days do I have left?", sid())
    g = await say(client, globex_employee, "How many leave days do I have left?", sid())
    assert "14.5 annual" in a["text"] and "14.0 annual" in g["text"]
    assert a["trace"][0]["name"] == "get_leave_balance"


async def test_policy_question_goes_through_search_tool(client, acme_employee):
    d = await say(client, acme_employee, "What is the wellness allowance?", sid())
    assert "Rs 1,000" in d["text"]
    assert d["citations"] and d["trace"][0]["name"] == "search_policies"


async def test_ticket_needs_confirmation_and_is_idempotent(client, fresh_employee):
    s = sid()
    d = await say(client, fresh_employee, "My laptop keyboard is broken, please raise a ticket", s)
    assert d["type"] == "confirm"
    action = d["action"]
    assert action["tool"] == "create_it_ticket" and action["args"]["category"] == "hardware"
    before = await get_pool().fetchval("SELECT count(*) FROM tickets WHERE user_id = $1", uuid.UUID(fresh_employee["user"]["id"]))
    assert before == 0
    r = await client.post("/api/agent/confirm", headers=fresh_employee["headers"], json={"session_id": s, "action_id": action["id"], "approve": True})
    assert r.status_code == 200
    assert "Ticket #" in r.json()["text"]
    again = await client.post("/api/agent/confirm", headers=fresh_employee["headers"], json={"session_id": s, "action_id": action["id"], "approve": True})
    assert again.status_code == 404
    tickets = (await client.get("/api/tickets", headers=fresh_employee["headers"])).json()
    assert len(tickets) == 1 and tickets[0]["priority"] in ("medium", "high")


async def test_declining_creates_nothing(client, fresh_employee):
    s = sid()
    d = await say(client, fresh_employee, "VPN is not working for me", s)
    r = await client.post("/api/agent/confirm", headers=fresh_employee["headers"], json={"session_id": s, "action_id": d["action"]["id"], "approve": False})
    assert "didn't raise" in r.json()["text"]
    assert (await client.get("/api/tickets", headers=fresh_employee["headers"])).json() == []


async def test_other_user_cannot_confirm_my_action(client, fresh_employee, acme_employee):
    s = sid()
    d = await say(client, fresh_employee, "Outlook keeps crashing, raise a ticket", s)
    r = await client.post("/api/agent/confirm", headers=acme_employee["headers"], json={"session_id": s, "action_id": d["action"]["id"], "approve": True})
    assert r.status_code == 404


async def test_new_message_cancels_pending_action(client, fresh_employee):
    s = sid()
    d = await say(client, fresh_employee, "I am locked out of my account", s)
    assert d["type"] == "confirm"
    d2 = await say(client, fresh_employee, "Actually, what are the core hours?", s)
    assert d2["type"] == "answer"
    r = await client.post("/api/agent/confirm", headers=fresh_employee["headers"], json={"session_id": s, "action_id": d["action"]["id"], "approve": True})
    assert r.status_code == 404


async def test_bad_tool_args_are_sanitised():
    from app.agent.tools import ToolArgError, validate_ticket_args

    clean = validate_ticket_args({"category": "DROP TABLE", "priority": "critical!!", "summary": "  printer   on fire  "})
    assert clean == {"category": "other", "priority": "medium", "summary": "printer on fire"}
    try:
        validate_ticket_args({"summary": "no"})
        raise AssertionError("expected error")
    except ToolArgError:
        pass
