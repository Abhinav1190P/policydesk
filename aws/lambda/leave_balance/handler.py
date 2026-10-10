import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request

log = logging.getLogger()
log.setLevel(logging.INFO)

APP_URL = os.environ.get("POLICYDESK_URL", "").rstrip("/")
SECRET = os.environ.get("INTERNAL_TOOLS_SECRET", "")
TIMEOUT = float(os.environ.get("TIMEOUT_S", "8"))


def sign(body: bytes, ts: int) -> str:
    return hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


def respond(event: dict, payload: dict, state: str | None = None) -> dict:
    body = {
        "actionGroup": event.get("actionGroup"),
        "function": event.get("function"),
        "functionResponse": {"responseBody": {"TEXT": {"body": json.dumps(payload)}}},
    }
    if state:
        body["functionResponse"]["responseState"] = state
    return {"messageVersion": "1.0", "response": body, "sessionAttributes": event.get("sessionAttributes", {}), "promptSessionAttributes": event.get("promptSessionAttributes", {})}


def call_app(path: str, data: dict) -> dict:
    body = json.dumps(data).encode()
    ts = int(time.time())
    req = urllib.request.Request(
        f"{APP_URL}{path}",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "X-PD-Timestamp": str(ts), "X-PD-Signature": sign(body, ts)},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == 2:
                raise
        except urllib.error.URLError:
            if attempt == 2:
                raise
        time.sleep(0.5 * (2 ** attempt))
    raise RuntimeError("unreachable")


def lambda_handler(event, context):
    attrs = event.get("sessionAttributes") or {}
    tenant_id = attrs.get("tenant_id")
    user_id = attrs.get("user_id")
    function = event.get("function")
    log.info(json.dumps({"function": function, "tenant_id": tenant_id, "session": event.get("sessionId")}))
    if function != "get_leave_balance":
        return respond(event, {"error": f"unsupported function {function}"}, "FAILURE")
    if not tenant_id or not user_id:
        return respond(event, {"error": "employee identity missing from session"}, "FAILURE")
    try:
        data = call_app("/internal/tools/leave-balance", {"tenant_id": tenant_id, "user_id": user_id})
    except Exception as e:
        log.exception("leave balance lookup failed")
        return respond(event, {"error": f"lookup failed: {e}"}, "FAILURE")
    return respond(event, data)
