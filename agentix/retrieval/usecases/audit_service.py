"""
审计日志服务——在高权限操作的事务内写审计记录。

spec §4.4-5：操作者 tenant/user、对象、结果齐备，不落敏感明文。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlmodel import Session

from agentix.retrieval.domain.models.audit import AuditLog

logger = logging.getLogger("agentix.audit")

_SENSITIVE_KEYS = {"password", "password_hash", "token", "secret", "api_key"}


def _sanitize_details(details: dict[str, Any] | None) -> str | None:
    """移除敏感字段后序列化为 JSON 字符串。"""
    if not details:
        return None
    safe = {k: v for k, v in details.items() if k.lower() not in _SENSITIVE_KEYS}
    return json.dumps(safe, ensure_ascii=False, default=str)


def write_audit(
    session: Session,
    *,
    actor_user_id: str,
    actor_tenant_id: str | None,
    action: str,
    target_type: str,
    target_id: str,
    result: str = "success",
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """在当前事务内写审计日志（不独立 commit，由调用方事务控制）。"""
    record = AuditLog(
        actor_user_id=actor_user_id,
        actor_tenant_id=actor_tenant_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        result=result,
        details=_sanitize_details(details),
    )
    session.add(record)
    session.flush()
    logger.info(
        "audit.%s.%s",
        target_type,
        action,
        extra={
            "actor_user_id": actor_user_id,
            "actor_tenant_id": actor_tenant_id,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "result": result,
        },
    )
    return record
