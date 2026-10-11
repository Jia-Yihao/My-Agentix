"""
API 依赖注入：db session、request_id、JWT 鉴权等
"""
import os
import logging

from sqlmodel import Session

from agentix.retrieval.infra.db.session import get_session as _get_session
from agentix.retrieval.api.response import ErrorCode
from fastapi import Request, Depends

from agentix.retrieval.domain.models.user import User
from agentix.retrieval.errors import AppError

logger = logging.getLogger("agentix.retrieval.deps")


async def get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


DependsSession = Depends(_get_session)
"""FastAPI 依赖注入的数据库 session 快捷引用"""


def _extract_bearer_token(request: Request) -> str | None:
    """从 Authorization: Bearer <token> 头提取 token。"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header:
        return None
    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


def get_current_user(
    request: Request,
    session: Session = DependsSession,
) -> User:
    """解析 JWT Bearer token 返回 User。未带 token 或 token 失效返回 401。

    Cookie Session 回退：当 ``AUTH_COOKIE_SESSION_FALLBACK=true`` 时，
    若无 Bearer token 则尝试从 Cookie Session 读取（R2.3 回滚开关）。
    """
    token = _extract_bearer_token(request)

    if token:
        from agentix.security.jwt import safe_decode_access_token

        ctx = safe_decode_access_token(token)
        if ctx is None:
            raise AppError(
                error_code=ErrorCode.UNAUTHORIZED,
                message="token 无效或已过期",
                status_code=401,
            )
        user = session.get(User, ctx.user_id)
        if not user:
            raise AppError(
                error_code=ErrorCode.UNAUTHORIZED,
                message="用户不存在",
                status_code=401,
            )
        if user.status == "disabled":
            raise AppError(
                error_code=ErrorCode.UNAUTHORIZED,
                message="账号已禁用",
                status_code=401,
            )
        return user

    cookie_fallback = os.getenv("AUTH_COOKIE_SESSION_FALLBACK", "").lower() in (
        "1", "true", "yes",
    )
    if cookie_fallback:
        uid = request.session.get("user_id")
        if uid:
            user = session.get(User, uid)
            if user and user.status != "disabled":
                return user

    raise AppError(
        error_code=ErrorCode.UNAUTHORIZED,
        message="未登录",
        status_code=401,
    )


def get_current_admin(
    request: Request,
    session: Session = DependsSession,
) -> User:
    """要求当前用户为 admin 角色。非 admin 返回 403。"""
    user = get_current_user(request, session)
    if user.role != "admin":
        raise AppError(
            error_code=ErrorCode.UNAUTHORIZED,
            message="需要管理员权限",
            status_code=403,
        )
    return user


def get_document_service(session: Session = DependsSession):
    from agentix.retrieval.usecases.document_service import DocumentService

    return DocumentService(session)


def get_conversation_service(session: Session = DependsSession):
    from agentix.retrieval.usecases.conversation_service import ConversationService

    return ConversationService(session)


def get_chat_service(session: Session = DependsSession):
    from agentix.retrieval.usecases.chat_service import ChatService
    from agentix.retrieval.infra.cache.redis_client import get_cache_redis

    return ChatService(session, cache_redis=get_cache_redis())


def get_kb_service(session: Session = DependsSession):
    from agentix.retrieval.usecases.kb_service import KbService

    return KbService(session)


def get_index_service(session: Session = DependsSession):
    from agentix.retrieval.usecases.index_service import IndexService

    return IndexService(session)


def get_feedback_service(session: Session = DependsSession):
    from agentix.retrieval.usecases.feedback_service import FeedbackService

    return FeedbackService(session)


def get_auth_service(session: Session = DependsSession):
    from agentix.retrieval.usecases.auth_service import AuthService

    return AuthService(session)


def get_folder_service(session: Session = DependsSession):
    from agentix.retrieval.usecases.folder_service import FolderService

    return FolderService(session)


def get_tenant_service(session: Session = DependsSession):
    from agentix.retrieval.usecases.tenant_service import TenantService

    return TenantService(session)
