"""
对话问答 DTO
"""
from typing import Literal
from pydantic import BaseModel, Field

from agentix.config.settings import settings


class ChatRequest(BaseModel):
    """问答请求"""
    kb_id: str
    # 企业化 P2-4.5：统一长文本输入上限（QUERY_MAX_LENGTH，防 token 放大攻击）
    question: str = Field(min_length=1, max_length=settings.QUERY_MAX_LENGTH)
    conversation_id: str | None = None
    document_id: str | None = None
    folder_id: str | None = None
    chat_mode: Literal["fast", "deep"] = Field(
        default="fast",
        description="fast=快速（无思考过程展示）；deep=深度（流式展示思考过程）",
    )


class Citation(BaseModel):
    """引用信息"""
    chunk_id: str
    document_id: str
    page_no: int | None = None
    snippet: str
    source: str | None = None
    # 文件级元信息（取不到时 None）
    document_title: str | None = None
    file_name: str | None = None
    folder_path: str | None = None
    file_size: int | None = None
    file_ext: str | None = None
