"""
模型配置统一管理 API（LLM + Embedding + Chunk 参数）

支持通过前端 WebUI 读写模型配置，覆盖环境变量默认值。
配置存储在 DB（ModelConfig 表），以 kb_id 为粒度隔离。
"""

import os
from typing import Any

from fastapi import APIRouter, Depends
from sqlmodel import Session, select, Field, SQLModel
from sqlalchemy import Column, JSON

from agentix.retrieval.api.deps import get_current_user, DependsSession
from agentix.retrieval.domain.models.user import User

router = APIRouter(dependencies=[Depends(get_current_user)])


# ============================================================
# 配置定义
# ============================================================

DEFAULT_CONFIG: dict[str, Any] = {
    # LLM
    "llm_provider": os.getenv("LLM_PROVIDER", "openai-compatible"),
    "llm_base_url": os.getenv("LLM_BASE_URL", "http://127.0.0.1:1234/v1"),
    "llm_api_key": os.getenv("LLM_API_KEY", ""),
    "llm_model": os.getenv("LLM_MODEL", "qwen2.5-7b-instruct"),
    # Embedding
    "embedding_provider": os.getenv("EMBEDDING_PROVIDER", "openai-compatible"),
    "embedding_base_url": os.getenv("EMBEDDING_BASE_URL", "http://127.0.0.1:1234/v1"),
    "embedding_api_key": os.getenv("EMBEDDING_API_KEY", ""),
    "embedding_model": os.getenv("EMBEDDING_MODEL", "text-embedding-bge-m3"),
    # Reranker
    "reranker_model": os.getenv("RERANKER_MODEL", "text-embedding-bge-reranker-v2-m3"),
    # Chunk
    "chunk_strategy": os.getenv("CHUNK_STRATEGY", "auto"),
    "chunk_mode": os.getenv("CHUNK_MODE", "single"),
    "chunk_size": int(os.getenv("PDF_CHUNK_SIZE", "500")),
    "chunk_overlap": int(os.getenv("PDF_CHUNK_OVERLAP", "100")),
    # Retrieval
    "search_k": int(os.getenv("SEARCH_K", "5")),
    "sparse_k": int(os.getenv("SPARSE_K", "10")),
    "dense_k": int(os.getenv("DENSE_K", "10")),
    "fusion_k": int(os.getenv("FUSION_K", "15")),
    "reranker_type": os.getenv("RERANKER_TYPE", "embedding"),
}


def get_merged_config(session: Session, kb_id: str | None = None) -> dict[str, Any]:
    """获取合并后的配置：默认值 ← 全局环境变量 ← KB 级别覆盖（来自 DB）。"""
    config = dict(DEFAULT_CONFIG)
    if kb_id:
        row = session.exec(
            select(ModelConfig).where(ModelConfig.kb_id == kb_id)
        ).first()
        if row and row.config:
            config.update(row.config)
    return config


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
    """获取指定知识库的完整配置（默认值 + 自定义覆盖）。"""
    return {"data": get_merged_config(session, kb_id)}


@router.put("/settings/{kb_id}")
def update_settings(
    kb_id: str,
    body: dict[str, Any],
    session: Session = DependsSession,
    current_user: User = Depends(get_current_user),
) -> dict:
    """更新指定知识库的配置覆盖项。"""
    row = session.exec(select(ModelConfig).where(ModelConfig.kb_id == kb_id)).first()
    if not row:
        row = ModelConfig(kb_id=kb_id, config={})
        session.add(row)
    row.config.update(body)
    session.add(row)
    session.commit()
    return {"data": get_merged_config(session, kb_id)}
