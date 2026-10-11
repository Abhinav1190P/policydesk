from app.config import get_settings


async def test_login_and_me(client, acme_employee):
    r = await client.get("/api/auth/me", headers=acme_employee["headers"])
    assert r.status_code == 200
    me = r.json()
    assert me["email"] == "employee@acme.example" and me["tenant_slug"] == "acme" and me["role"] == "employee"


async def test_wrong_password(client, services):
    r = await client.post("/api/auth/login", json={"email": "employee@acme.example", "password": "nope-nope"})
    assert r.status_code == 401


async def test_register_needs_valid_invite(client, services):
    body = {"email": "x1@acme.example", "password": "Password123!", "name": "X", "tenant_slug": "acme", "invite_code": "WRONG-CODE"}
    assert (await client.post("/api/auth/register", json=body)).status_code == 400
    body["invite_code"] = "ACME-2026"
    assert (await client.post("/api/auth/register", json=body)).status_code == 201
    assert (await client.post("/api/auth/register", json=body)).status_code == 409


async def test_requires_token(client, services):
    assert (await client.get("/api/conversations")).status_code == 401
    bad = {"Authorization": "Bearer not-a-token"}
    assert (await client.get("/api/conversations", headers=bad)).status_code == 401


async def test_employee_cannot_use_admin_routes(client, acme_employee):
    assert (await client.get("/api/admin/usage", headers=acme_employee["headers"])).status_code == 403
    files = {"file": ("x.md", b"# X\n\nSome text here for the doc.", "text/markdown")}
    assert (await client.post("/api/documents", headers=acme_employee["headers"], files=files)).status_code == 403


async def test_login_rate_limited(client, services, monkeypatch):
    monkeypatch.setattr(get_settings(), "login_attempts_per_minute", 3)
    codes = [
        (await client.post("/api/auth/login", json={"email": "ratelimit@acme.example", "password": "x"}, headers={"x-forwarded-for": "10.9.9.9"})).status_code
        for _ in range(4)
    ]
    assert codes[:3] == [401, 401, 401] and codes[3] == 429


async def test_health(client, services):
    r = await client.get("/api/health")
    assert r.status_code == 200 and r.json()["checks"] == {"postgres": "ok", "redis": "ok", "rabbitmq": "ok"}
    assert "X-Instance" in r.headers


async def test_ping_answers_head_for_uptime_monitors(client):
    assert (await client.head("/api/ping")).status_code == 200
    assert (await client.get("/api/ping")).json()["ok"] is True
