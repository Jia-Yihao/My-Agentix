"""
v1 逐端点鉴权依赖（接口审查批次 2026-09-27）。

分层：
- 用户面（chat / sessions / upload 写操作）：``V1_REQUIRE_JWT`` 开启时要求有效
  Bearer JWT（只验签 + 过期 + type=access，不查库——v1 是轻量域，区别于
  retrieval 域的 ``get_current_user`` 需查 User 行）。默认关 = 匿名行为不变
  （strangler flag-off 字节等价）。
- 机器面（events / ops）：复用既有 ``verify_api_key``（``REQUIRE_API_KEY`` 开启
  时校验 x-api-key，与全局 ``APIKeyMiddleware`` 同口径）——为**未挂中间件的
  装配面**（轻量 app、嵌入式挂载）兜底；中间件覆盖的请求则双保险。

归属校验（审计批次 A1/A2，2026-09-28）：
- ``resolve_v1_caller``：调用方身份声明——JWT regime（``V1_REQUIRE_JWT`` 开）
  取 **JWT sub**（服务端签发，强身份）；匿名 regime 取 ``X-Device-ID`` 头
  （客户端声明，弱身份但足以把「会话 UUID + 归属 user_id」双因子化）；
  两者皆无 → ``None``（匿名轻量域 = 既有行为）。
- ``session_visible_to``：归属判定，失败端点回 404（防存在性枚举）。
  身份期之前的 ``default_user`` 会话是**无主匿名桶**，不随身份迁移
  （引入 header/JWT 后新建会话归属声明的身份）。
"""
from typing import Optional

from fastapi import HTTPException, Request, status

from agentix.config import settings
from agentix.security.jwt import safe_decode_access_token

#: 审计批次 A1：会话归属声明头（客户端携带的设备身份）
DEVICE_ID_HEADER = "X-Device-ID"

#: 前身份期的匿名桶 owner（历史会话 user_id；视为无主，见模块 docstring）
LEGACY_ANONYMOUS_USER_ID = "default_user"


def require_v1_user(request: Request) -> None:
    """``V1_REQUIRE_JWT`` 开启时校验 Bearer JWT，未开启则直通。"""
    if not settings.V1_REQUIRE_JWT:
        return
    auth: Optional[str] = request.headers.get("Authorization")
    token: Optional[str] = None
    if auth and auth.lower().startswith("bearer "):
        token = auth[7:].strip() or None
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if safe_decode_access_token(token) is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def resolve_v1_caller(request: Request) -> Optional[str]:
    """解析调用方身份声明（审计批次 A1）。

    - ``V1_REQUIRE_JWT`` 开：JWT sub（``require_v1_user`` 已先行验签；
      解析不到时返回 ``None``，由端点归属判定兜底 404，绝不放行）。
    - 关：``X-Device-ID`` 头（strip 后非空）；无头 = ``None`` = 匿名轻量域
      （既有行为不变——header 是**可选**声明，出现即启用归属校验）。
    """
    if settings.V1_REQUIRE_JWT:
        auth = request.headers.get("Authorization")
        if auth and auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            if token:
                ctx = safe_decode_access_token(token)
                if ctx is not None:
                    return ctx.user_id
        return None
    device = (request.headers.get(DEVICE_ID_HEADER) or "").strip()
    return device or None


def session_visible_to(owner: Optional[str], caller: Optional[str]) -> bool:
    """会话归属判定（审计批次 A1）。

    - ``caller is None``：匿名轻量域，行为不变（strangler 纪律）。
    - ``owner == caller``：归属本人。
    - ``owner is None``：user_id 为空的历史行（无主）。
    - ``owner == LEGACY_ANONYMOUS_USER_ID``：匿名桶（见模块 docstring）。
    - 其余：跨用户 → 端点回 404。
    """
    if caller is None:
        return True
    if owner is None or owner == caller:
        return True
    return owner == LEGACY_ANONYMOUS_USER_ID
