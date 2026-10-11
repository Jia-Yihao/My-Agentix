"""
JWT 认证工具——统一签发与校验 access_token（HS256）。

复用 `agentix.config.settings.SECRET_KEY` + `ALGORITHM`，
生产环境 SECRET_KEY 缺失时 `resolved_secret_key` 直接拒绝启动。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from jwt import InvalidTokenError, ExpiredSignatureError

from agentix.config import settings

logger = logging.getLogger("agentix.security.jwt")

TOKEN_TYPE_ACCESS = "access"


@dataclass(frozen=True)
class UserContext:
    """鉴权上下文——由 JWT 解析得到，注入 usecases 层不感知 HTTP。"""

    user_id: str
    tenant_id: str | None = None
    role: str = "member"
    status: str = "active"


def create_access_token(
    *,
    user_id: str,
    tenant_id: str | None = None,
    role: str = "member",
    status: str = "active",
    expires_delta: timedelta | None = None,
) -> str:
    """签发 JWT access_token。"""
    if expires_delta is None:
        expires_delta = timedelta(
            seconds=int(getattr(settings, "ACCESS_TOKEN_EXPIRE_SECONDS", "86400"))
        )
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "role": role,
        "status": status,
        "type": TOKEN_TYPE_ACCESS,
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, settings.resolved_secret_key, algorithm=settings.ALGORITHM)


def decode_access_token(token: str) -> UserContext:
    """校验 JWT access_token 并返回 UserContext。

    Raises:
        InvalidTokenError: token 无效或过期。
    """
    payload = jwt.decode(
        token,
        settings.resolved_secret_key,
        algorithms=[settings.ALGORITHM],
    )
    if payload.get("type") != TOKEN_TYPE_ACCESS:
        raise InvalidTokenError("token type mismatch")
    return UserContext(
        user_id=payload["sub"],
        tenant_id=payload.get("tenant_id"),
        role=payload.get("role", "member"),
        status=payload.get("status", "active"),
    )


def safe_decode_access_token(token: str) -> UserContext | None:
    """校验 JWT，失败返回 None（不抛异常）。"""
    try:
        return decode_access_token(token)
    except (InvalidTokenError, ExpiredSignatureError, KeyError) as exc:
        logger.debug("jwt decode failed: %s", exc)
        return None
