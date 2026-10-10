import hmac
import uuid

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import get_settings
from app.db.pool import get_pool
from app.infra import ratelimit
from app.routes.deps import client_ip
from app.security import CurrentUser, create_token, current_user, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterIn(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=80)
    tenant_slug: str = Field(min_length=2, max_length=40)
    invite_code: str = Field(min_length=4, max_length=64)


class LoginIn(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)
    password: str = Field(min_length=1, max_length=128)


def user_out(u: dict) -> dict:
    return {"id": str(u["id"]), "email": u["email"], "name": u["name"], "role": u["role"], "tenant_id": str(u["tenant_id"]), "tenant_slug": u["tenant_slug"], "tenant_name": u.get("tenant_name")}


async def _login_limit(request: Request, email: str) -> None:
    s = get_settings()
    for key in (f"login:ip:{client_ip(request)}", f"login:email:{email.lower()}"):
        r = await ratelimit.take(key, s.login_attempts_per_minute, s.login_attempts_per_minute)
        if not r.allowed:
            raise HTTPException(429, "Too many login attempts, try again in a minute", headers={"Retry-After": str(max(1, r.retry_after_ms // 1000))})


@router.post("/register", status_code=201)
async def register(body: RegisterIn, request: Request):
    await _login_limit(request, body.email)
    pool = get_pool()
    tenant = await pool.fetchrow("SELECT id, slug, name, invite_code FROM tenants WHERE slug = $1", body.tenant_slug.lower())
    if tenant is None or not hmac.compare_digest(tenant["invite_code"], body.invite_code):
        raise HTTPException(400, "Company code or invite code is wrong")
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                u = await conn.fetchrow(
                    "INSERT INTO users (tenant_id, email, name, password_hash, role) VALUES ($1, $2, $3, $4, 'employee') RETURNING id, tenant_id, email, name, role",
                    tenant["id"], body.email.lower(), body.name.strip(), hash_password(body.password),
                )
                await conn.execute(
                    "INSERT INTO leave_balances (user_id, tenant_id, annual_total, sick_total) VALUES ($1, $2, 20, 10)",
                    u["id"], tenant["id"],
                )
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "An account with this email already exists")
    user = {**dict(u), "tenant_slug": tenant["slug"], "tenant_name": tenant["name"]}
    return {"token": create_token(user), "user": user_out(user)}


@router.post("/login")
async def login(body: LoginIn, request: Request):
    await _login_limit(request, body.email)
    row = await get_pool().fetchrow(
        """
        SELECT u.id, u.tenant_id, u.email, u.name, u.role, u.password_hash, t.slug AS tenant_slug, t.name AS tenant_name
        FROM users u JOIN tenants t ON t.id = u.tenant_id WHERE lower(u.email) = lower($1)
        """,
        body.email,
    )
    if row is None or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(401, "Email or password is wrong")
    user = dict(row)
    return {"token": create_token(user), "user": user_out(user)}


@router.get("/me")
async def me(user: CurrentUser = Depends(current_user)):
    row = await get_pool().fetchrow(
        "SELECT u.id, u.tenant_id, u.email, u.name, u.role, t.slug AS tenant_slug, t.name AS tenant_name FROM users u JOIN tenants t ON t.id = u.tenant_id WHERE u.id = $1",
        uuid.UUID(user.id),
    )
    if row is None:
        raise HTTPException(401, "User no longer exists")
    return user_out(dict(row))
