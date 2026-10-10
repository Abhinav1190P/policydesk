import importlib.util
import io
import json
import time
from pathlib import Path

from app.security import sign_internal, verify_internal
from tests.conftest import get_pool

LAMBDA = Path(__file__).resolve().parents[2] / "aws" / "lambda" / "leave_balance" / "handler.py"


def signed(body: dict, secret="internal-test-secret", ts=None):
    raw = json.dumps(body).encode()
    ts = ts or int(time.time())
    return raw, {"X-PD-Timestamp": str(ts), "X-PD-Signature": sign_internal(raw, ts, secret), "Content-Type": "application/json"}


async def ids(email):
    row = await get_pool().fetchrow("SELECT id, tenant_id FROM users WHERE email = $1", email)
    return str(row["tenant_id"]), str(row["id"])


async def test_signed_request_returns_balance(client, services):
    tenant_id, user_id = await ids("employee@acme.example")
    raw, headers = signed({"tenant_id": tenant_id, "user_id": user_id})
    r = await client.post("/internal/tools/leave-balance", content=raw, headers=headers)
    assert r.status_code == 200 and r.json()["annual_remaining"] == 14.5


async def test_bad_signature_and_stale_timestamp_rejected(client, services):
    tenant_id, user_id = await ids("employee@acme.example")
    raw, headers = signed({"tenant_id": tenant_id, "user_id": user_id}, secret="wrong")
    assert (await client.post("/internal/tools/leave-balance", content=raw, headers=headers)).status_code == 401
    raw, headers = signed({"tenant_id": tenant_id, "user_id": user_id}, ts=int(time.time()) - 3600)
    assert (await client.post("/internal/tools/leave-balance", content=raw, headers=headers)).status_code == 401
    assert (await client.post("/internal/tools/leave-balance", content=raw)).status_code == 401


async def test_tenant_user_mismatch_rejected(client, services):
    acme_tenant, _ = await ids("employee@acme.example")
    _, globex_user = await ids("employee@globex.example")
    raw, headers = signed({"tenant_id": acme_tenant, "user_id": globex_user})
    assert (await client.post("/internal/tools/leave-balance", content=raw, headers=headers)).status_code == 404


def load_lambda(monkeypatch):
    monkeypatch.setenv("POLICYDESK_URL", "https://policydesk.example")
    monkeypatch.setenv("INTERNAL_TOOLS_SECRET", "internal-test-secret")
    spec = importlib.util.spec_from_file_location("leave_lambda", LAMBDA)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_lambda_signs_requests_with_session_identity(monkeypatch):
    mod = load_lambda(monkeypatch)
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["body"] = req.data
        verify_internal(req.data, req.get_header("X-pd-timestamp"), req.get_header("X-pd-signature"))
        return Resp(json.dumps({"annual_remaining": 10}).encode())

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    event = {
        "actionGroup": "employee_tools",
        "function": "get_leave_balance",
        "parameters": [{"name": "user_id", "type": "string", "value": "someone-else"}],
        "sessionAttributes": {"tenant_id": "t-1", "user_id": "u-1"},
    }
    out = mod.lambda_handler(event, None)
    assert seen["url"] == "https://policydesk.example/internal/tools/leave-balance"
    assert json.loads(seen["body"]) == {"tenant_id": "t-1", "user_id": "u-1"}
    body = json.loads(out["response"]["functionResponse"]["responseBody"]["TEXT"]["body"])
    assert body == {"annual_remaining": 10}
    assert out["messageVersion"] == "1.0" and out["response"]["function"] == "get_leave_balance"


def test_lambda_refuses_without_session_identity(monkeypatch):
    mod = load_lambda(monkeypatch)
    out = mod.lambda_handler({"actionGroup": "employee_tools", "function": "get_leave_balance", "sessionAttributes": {}}, None)
    assert out["response"]["functionResponse"]["responseState"] == "FAILURE"
