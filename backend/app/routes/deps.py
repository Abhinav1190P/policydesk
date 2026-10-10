from fastapi import Depends, HTTPException, Request

from app.config import get_settings
from app.infra import ratelimit
from app.security import CurrentUser, current_user


async def ai_rate_limit(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    s = get_settings()
    per_user = await ratelimit.take(f"user:{user.id}", s.rate_limit_per_minute, s.rate_limit_burst)
    if not per_user.allowed:
        raise HTTPException(429, "Too many requests, slow down a little", headers={"Retry-After": str(max(1, per_user.retry_after_ms // 1000))})
    per_tenant = await ratelimit.take(f"tenant:{user.tenant_id}", s.tenant_rate_limit_per_minute, s.tenant_rate_limit_per_minute // 4 or 1)
    if not per_tenant.allowed:
        raise HTTPException(429, "Your company has hit its request limit for now", headers={"Retry-After": str(max(1, per_tenant.retry_after_ms // 1000))})
    return user


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
