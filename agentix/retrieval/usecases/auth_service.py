"""
注册 / 登录业务编排（不依赖 Request；Session Cookie 在路由层写入）
"""
import os
import logging

from sqlmodel import Session, select

from agentix.retrieval.api.response import ErrorCode
from agentix.retrieval.domain.models.tenant import Tenant
from agentix.retrieval.domain.models.user import User
from agentix.retrieval.errors import AppError
from agentix.retrieval.infra.auth.password import hash_password, verify_password
from agentix.retrieval.infra.repos import user_repo

logger = logging.getLogger("agentix.retrieval.auth")


def _get_default_tenant_id(session: Session) -> str:
    """获取默认租户 ID（新注册用户自动归属此租户，R2.7 后由注册接口显式指定）。"""
    default_name = os.getenv("LEGELBOT_DEFAULT_TENANT_NAME", "default")
    tenant = session.exec(select(Tenant).where(Tenant.name == default_name)).first()
    if not tenant:
        tenant = Tenant(name=default_name)
        session.add(tenant)
        session.commit()
        session.refresh(tenant)
    return tenant.id


class AuthService:
    def __init__(self, session: Session):
        self._session = session

    def register(self, email: str, username: str, password: str) -> User:
        if user_repo.get_user_by_email(self._session, email):
            raise AppError(
                error_code=ErrorCode.EMAIL_ALREADY_REGISTERED,
                message="该邮箱已注册",
                status_code=409,
            )
        if user_repo.get_user_by_username(self._session, username):
            raise AppError(
                error_code=ErrorCode.NAME_CONFLICT,
                message="用户名已被占用",
                status_code=409,
            )
        tenant_id = _get_default_tenant_id(self._session)
        user = user_repo.create_user(
            self._session,
            email=email,
            username=username,
            password_hash=hash_password(password),
            tenant_id=tenant_id,
        )
        logger.info("user.registered", extra={"user_id": user.id, "tenant_id": tenant_id})
        return user

    def login(self, email: str, password: str) -> User:
        user = user_repo.get_user_by_email(self._session, email)
        if not user or not verify_password(password, user.password_hash):
            raise AppError(
                error_code=ErrorCode.AUTH_INVALID_CREDENTIALS,
                message="邮箱或密码错误",
                status_code=401,
            )
        if user.status == "disabled":
            raise AppError(
                error_code=ErrorCode.UNAUTHORIZED,
                message="账号已禁用",
                status_code=401,
            )
        return user
