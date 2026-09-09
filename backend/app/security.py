"""双通道鉴权。

- 通道二 `/api/admin/*`：JWT Bearer → `current_user`
- 通道一 `/api/v1/*`：`X-API-Key`（开发期可降级为 JWT）→ `api_caller`

后端不做多语言协商，错误信息一律为英文 `code: message`。
"""

import base64
import hashlib
import hmac
import os
import time
from typing import Optional

import jwt
from fastapi import Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .errors import EXIT_GATE_BLOCKED, AppError
from .models import User

ALGORITHM = "HS256"
PBKDF2_ITERATIONS = 260_000

_bearer = HTTPBearer(auto_error=False)


# ---------- 密码 ----------
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2$%d$%s$%s" % (
        PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, iterations, salt_b64, digest_b64 = encoded.split("$")
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            base64.b64decode(salt_b64),
            int(iterations),
        )
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except Exception:
        return False


# ---------- JWT ----------
def create_access_token(user: User) -> str:
    now = int(time.time())
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "iat": now,
        "exp": now + settings.JWT_EXPIRE_HOURS * 3600,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


def _user_from_token(credentials: Optional[HTTPAuthorizationCredentials], db: Session) -> Optional[User]:
    if credentials is None:
        return None
    payload = decode_token(credentials.credentials)
    if not payload:
        return None
    try:
        sub = payload.get("sub")
        if sub is None:
            return None
        return db.get(User, int(sub))
    except (TypeError, ValueError):
        return None


# ---------- 依赖 ----------
def current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    user = _user_from_token(credentials, db)
    if user is None:
        raise AppError(
            status.HTTP_401_UNAUTHORIZED,
            "authentication_required",
            "Please login first",
            exit_code=EXIT_GATE_BLOCKED,
        )
    return user


def api_caller(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    db: Session = Depends(get_db),
) -> str:
    """通道一闸门：X-API-Key 或网页 JWT 二选一，返回来源标识。"""
    provided = request.headers.get("x-api-key", "").strip()
    if settings.V1_API_KEY and hmac.compare_digest(provided, settings.V1_API_KEY):
        return "api_key_client"

    user = _user_from_token(credentials, db)
    if user is not None:
        return user.full_name or user.username

    raise AppError(
        status.HTTP_401_UNAUTHORIZED,
        "invalid_credentials",
        "Missing X-API-Key or valid JWT",
        exit_code=EXIT_GATE_BLOCKED,
    )
