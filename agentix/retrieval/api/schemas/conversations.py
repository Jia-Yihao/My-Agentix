"""
会话 / 消息 / 反馈 DTO
"""
from datetime import datetime
from typing import List
from pydantic import BaseModel


class CitationData(BaseModel):
    chunk_id: str
    document_id: str | None = None
    page_no: int | None = None
    snippet: str = ""
    source: str | None = None
    # 效力状态（实时 SSE 一直有；历史 DTO 缺失 → 刷新后徽标消失，Phase 23 补齐）
    effective_status: str | None = None
    # 失效法条的替代信息（前端「已被…替代」提示；老数据无此键 → None）
    replaced_by: str | None = None
    # 文件级元信息（历史数据无此键 → None）
    document_title: str | None = None
    file_name: str | None = None
    folder_path: str | None = None
    file_size: int | None = None
    file_ext: str | None = None


class MessageData(BaseModel):
    id: str
    role: str
    content: str
    answer_id: str | None = None
    created_at: datetime
    citations: List[CitationData] = []


class ConversationData(BaseModel):
    id: str
    kb_id: str
    title: str
    scope_type: str = "kb"
    scope_id: str | None = None
    created_at: datetime
    updated_at: datetime
    messages: List[MessageData] = []


class ConversationRenameRequest(BaseModel):
    title: str
