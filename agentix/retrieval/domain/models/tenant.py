"""
租户表——最高隔离层，用户与知识库均归属租户。
"""
from datetime import datetime

from agentix.retrieval.domain.datetime_utils import utc_now
from agentix.retrieval.domain.models.user import _uuid

from sqlmodel import Field, SQLModel


class Tenant(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    name: str = Field(max_length=128, index=True)
    status: str = Field(default="active", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
