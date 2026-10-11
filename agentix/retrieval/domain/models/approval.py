"""
审批表族：删除类危险操作审批

状态流转（单向）：pending → approved / rejected / timeout
- pending：删除请求已提交，等待审批人处理；
- approved：审批通过，文档进入 deleted_pending_purge 并排队重建；
- rejected：审批驳回，文档恢复就绪状态，删除请求终止；
- timeout：超过 DELETE_APPROVAL_TIMEOUT_SECONDS 未处理（仍允许审批人处理）。
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import Index
from sqlmodel import Field, SQLModel

from agentix.retrieval.domain.datetime_utils import utc_now


def _uuid() -> str:
    return uuid4().hex


class DeleteApproval(SQLModel, table=True):
    """删除类危险操作审批记录"""

    __table_args__ = (
        Index("ix_delete_approval_status_created", "status", "created_at"),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    target_type: str = Field(description="document|kb")
    target_id: str = Field(index=True)
    status: str = Field(default="pending", description="pending/approved/rejected/timeout")
    requested_by: str = Field(index=True)
    approved_by: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)
    processed_at: datetime | None = Field(default=None)