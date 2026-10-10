import hashlib
import hmac
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.config import get_settings

bearer = HTTPBearer(auto_error=False)


@dataclass
class CurrentUser:
    id: str
    tenant_id: str
    tenant_slug: str
    email: str
    name: str
    role: str


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=10)).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except ValueError:
        return False


def create_token(user: dict) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user["id"]),
        "tid": str(user["tenant_id"]),
        "tslug": user["tenant_slug"],
        "email": user["email"],
        "name": user["name"],
        "role": user["role"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=s.jwt_ttl_minutes)).timestamp()),
    }
    return jwt.encode(payload, s.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> CurrentUser:
    try:
        p = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return CurrentUser(p["sub"], p["tid"], p["tslug"], p["email"], p["name"], p["role"])


async def current_user(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> CurrentUser:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    user = decode_token(creds.credentials)
    request.state.user = user
    return user


async def admin_user(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return user


def sign_internal(body: bytes, ts: int, secret: str | None = None) -> str:
    key = (secret or get_settings().internal_tools_secret).encode()
    return hmac.new(key, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


def verify_internal(body: bytes, ts_header: str | None, sig_header: str | None) -> None:
    s = get_settings()
    if not ts_header or not sig_header:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing signature")
    try:
        ts = int(ts_header)
    except ValueError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad timestamp")
    if abs(time.time() - ts) > s.internal_tools_max_skew_s:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Stale request")
    if not hmac.compare_digest(sign_internal(body, ts), sig_header):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad signature")
