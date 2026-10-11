"""
审批相关 Pydantic DTO
"""
from datetime import datetime
from pydantic import BaseModel


class ApprovalData(BaseModel):
    id: str
    target_type: str
    target_id: str
    status: str
    requested_by: str
    approved_by: str | None = None
    created_at: datetime
    processed_at: datetime | None = None
    timed_out: bool = False


class ApprovalListData(BaseModel):
    items: list[ApprovalData]
    total: int


class ApprovalActionData(BaseModel):
    approval_id: str
    status: str
    target_gone: bool = False
    job_id: str | None = None
    should_enqueue: bool = False
