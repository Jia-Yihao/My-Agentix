"""
租户内 RBAC：admin / member 角色授权。

- admin：可管理（增删改）租户内资源，包括删除知识库、管理成员
- member：可读写授权范围内资源（创建知识库、上传文档、问答），不可删除

角色由 JWT `role` 声明携带，由 `UserContext.role` 注入 usecases 层。
"""
from __future__ import annotations

from agentix.retrieval.errors import AppError
from agentix.retrieval.api.response import ErrorCode, get_status_code

ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
VALID_ROLES = {ROLE_ADMIN, ROLE_MEMBER}


def require_admin(role: str | None) -> None:
    """要求 admin 角色，否则 403。"""
    if role != ROLE_ADMIN:
        raise AppError(
            error_code=ErrorCode.FORBIDDEN,
            message="需要管理员权限",
            status_code=get_status_code(ErrorCode.FORBIDDEN),
        )


def require_admin_or_member(role: str | None) -> None:
    """要求 admin 或 member 角色，否则 403。"""
    if role not in VALID_ROLES:
        raise AppError(
            error_code=ErrorCode.FORBIDDEN,
            message="无权执行此操作",
            status_code=get_status_code(ErrorCode.FORBIDDEN),
        )


def can_delete(role: str | None) -> bool:
    """删除操作仅 admin 可执行。"""
    return role == ROLE_ADMIN


def can_upload(role: str | None) -> bool:
    """上传操作 admin 和 member 均可。"""
    return role in VALID_ROLES


def can_manage_tenant(role: str | None) -> bool:
    """租户管理操作仅 admin 可执行。"""
    return role == ROLE_ADMIN
