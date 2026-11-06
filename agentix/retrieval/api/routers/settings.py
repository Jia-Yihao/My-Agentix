"""
模型配置统一管理 API（LLM + Embedding + Chunk 参数）

支持通过前端 WebUI 读写模型配置，覆盖环境变量默认值。
配置存储在 DB（ModelConfig 表），以 kb_id 为粒度隔离。

授权（§24.3 T1 ⑪，`SETTINGS_AUTHZ_ENABLED` 默认关）：
- 关：与加固前逐位等价——只要登录即可读改任意 `kb_id`。
- 开：读要求该 kb 属于调用者租户（不存在 404 / 跨租户 403），写在此之上再要求 admin。
"""

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select, Field, SQLModel
from sqlalchemy import Column, JSON

from agentix.config.settings import settings as app_settings
from agentix.retrieval.api.authz import require_kb_in_tenant
from agentix.retrieval.api.deps import get_current_user, DependsSession
from agentix.retrieval.api.rbac import require_admin
from agentix.retrieval.domain.models.user import User

router = APIRouter(dependencies=[Depends(get_current_user)])


# ============================================================
# 配置定义
# ============================================================

DEFAULT_CONFIG: dict[str, Any] = {
    # LLM
    "llm_provider": app_settings.LLM_PROVIDER,
    "llm_base_url": app_settings.LLM_BASE_URL or "",
    "llm_api_key": app_settings.LLM_API_KEY or "",
    "llm_model": app_settings.CHAT_MODEL or app_settings.LLM_MODEL,
    # Embedding
    "embedding_provider": app_settings.EMBEDDING_PROVIDER,
    "embedding_base_url": app_settings.EMBEDDING_BASE_URL or "",
    "embedding_api_key": app_settings.EMBEDDING_API_KEY or "",
    "embedding_model": app_settings.EMBEDDING_MODEL,
    # Chunk
    "chunk_strategy": os.getenv("CHUNK_STRATEGY", "auto"),
    "chunk_mode": os.getenv("CHUNK_MODE", "single"),
    "chunk_size": app_settings.KB_CHUNK_SIZE,
    "chunk_overlap": app_settings.KB_CHUNK_OVERLAP,
    # Retrieval
    "search_k": app_settings.KB_TOP_K,
}

#: PUT 只允许这些键，避免任意负载把无关结构写进 model_config 表。
ALLOWED_CONFIG_KEYS = frozenset(DEFAULT_CONFIG)


#: 密钥类字段名后缀：这些键的值只写不读，接口返回前一律掩码。
SECRET_KEY_SUFFIXES = ("_api_key", "_secret", "_token")
MASKED_PREFIX = "***"


def is_secret_field(name: str) -> bool:
    return name.endswith(SECRET_KEY_SUFFIXES)


def mask_secret(value: Any) -> Any:
    """保留末 4 位便于辨认「配的是哪个 key」，其余打掉。"""
    if not isinstance(value, str) or not value:
        return value
    if value.startswith(MASKED_PREFIX):
        return value
    tail = value[-4:] if len(value) > 4 else ""
    return f"{MASKED_PREFIX}{tail}"


def mask_config(config: dict[str, Any]) -> dict[str, Any]:
    return {k: (mask_secret(v) if is_secret_field(k) else v) for k, v in config.items()}


def is_masked_value(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(MASKED_PREFIX)


def get_merged_config(session: Session, kb_id: str | None = None) -> dict[str, Any]:
    """获取合并后的配置（**密钥已掩码**）：默认值 ← 全局环境变量 ← KB 级别覆盖。"""
    config = dict(DEFAULT_CONFIG)
    if kb_id:
        row = session.exec(
            select(ModelConfig).where(ModelConfig.kb_id == kb_id)
        ).first()
        if row and row.config:
            config.update(
                {key: value for key, value in row.config.items() if key in ALLOWED_CONFIG_KEYS}
            )
    return mask_config(config)


# ============================================================
# DB 模型
# ============================================================


class ModelConfig(SQLModel, table=True):
    """知识库级别的模型配置覆盖。"""

    __tablename__ = "model_config"

    kb_id: str = Field(primary_key=True)
    config: dict = Field(default_factory=dict, sa_column=Column(JSON))


# ============================================================
# API 端点
# ============================================================


@router.get("/settings/{kb_id}")
def get_settings(
    kb_id: str,
    session: Session = DependsSession,
    current_user: User = Depends(get_current_user),
) -> dict:
    """获取指定知识库的完整配置（默认值 + 自定义覆盖，密钥已掩码）。"""
    if app_settings.SETTINGS_AUTHZ_ENABLED:
        require_kb_in_tenant(session, kb_id, current_user.tenant_id)
    return {"data": get_merged_config(session, kb_id)}


@router.put("/settings/{kb_id}")
def update_settings(
    kb_id: str,
    body: dict[str, Any],
    session: Session = DependsSession,
    current_user: User = Depends(get_current_user),
) -> dict:
    """更新指定知识库的配置覆盖项（只接受已知键；掩码值视为「未改动」）。"""
    if app_settings.SETTINGS_AUTHZ_ENABLED:
        require_kb_in_tenant(session, kb_id, current_user.tenant_id)
        require_admin(current_user.role)
    unknown = sorted(set(body) - ALLOWED_CONFIG_KEYS)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail={"code": "UNKNOWN_CONFIG_KEYS", "fields": unknown},
        )
    # 前端会把 GET 回来的掩码串原样 PUT 回来；这种值不能落库，否则把真 key 覆盖成 ***。
    updates = {
        k: v
        for k, v in body.items()
        if not (is_secret_field(k) and is_masked_value(v))
    }
    row = session.exec(select(ModelConfig).where(ModelConfig.kb_id == kb_id)).first()
    if not row:
        row = ModelConfig(kb_id=kb_id, config={})
        session.add(row)
    # 整体重新赋值，不做 `row.config.update(...)`：JSON 列没有挂 MutableDict 监听，
    # 原地改字典不会被 SQLAlchemy 的变更检测看见——「第一次 PUT」靠 INSERT 侥幸写进去了，
    # 「对同一 kb 的第二次及以后」则是回 200、一个新键都不落库（由 p112 实测钉住）。
    row.config = {**(row.config or {}), **updates}
    session.add(row)
    session.commit()
    return {"data": get_merged_config(session, kb_id)}
