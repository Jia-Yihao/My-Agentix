"""
注册 / 登录 / 登出——统一 JWT 认证（返回 access_token 给前端，由前端存 Authorization 头）

R2.3 回滚开关：``AUTH_COOKIE_SESSION_FALLBACK=true`` 时 deps.py 回退 Cookie Session。
"""
import os
from fastapi import APIRouter, Request, Depends

from agentix.retrieval.api.deps import get_auth_service
from agentix.retrieval.api.response import success
from agentix.retrieval.api.schemas.auth import RegisterRequest, LoginRequest, UserPublic, AuthTokenResponse
from agentix.retrieval.usecases.auth_service import AuthService
from agentix.security.jwt import create_access_token

router = APIRouter()


def _build_token_response(user) -> dict:
    token = create_access_token(
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
        status=user.status,
    )
    user_public = UserPublic(
        id=user.id,
        email=user.email,
        username=user.username,
        tenant_id=user.tenant_id,
        role=user.role,
        status=user.status,
    )
    return AuthTokenResponse(access_token=token, user=user_public).model_dump()


def _session_login(request: Request, user_id: str) -> None:
    """Cookie Session 回退路径（仅 AUTH_COOKIE_SESSION_FALLBACK=true 时有效）。"""
    try:
        request.session.clear()
        request.session["user_id"] = user_id
    except (RuntimeError, AttributeError):
        pass


@router.post("/auth/register")
def api_register(
    request: Request,
    req: RegisterRequest,
    svc: AuthService = Depends(get_auth_service),
):
    user = svc.register(email=str(req.email), username=req.username, password=req.password)
    if os.getenv("AUTH_COOKIE_SESSION_FALLBACK", "").lower() in ("1", "true", "yes"):
        _session_login(request, user.id)
    data = _build_token_response(user)
    return success(data=data, request_id=request.state.request_id)


@router.post("/auth/login")
def api_login(
    request: Request,
    req: LoginRequest,
    svc: AuthService = Depends(get_auth_service),
):
    user = svc.login(email=str(req.email), password=req.password)
    if os.getenv("AUTH_COOKIE_SESSION_FALLBACK", "").lower() in ("1", "true", "yes"):
        _session_login(request, user.id)
    data = _build_token_response(user)
    return success(data=data, request_id=request.state.request_id)


@router.post("/auth/logout")
def api_logout(request: Request):
    try:
        request.session.clear()
    except (RuntimeError, AttributeError):
        pass
    return success(data={"logged_out": True}, request_id=request.state.request_id)
