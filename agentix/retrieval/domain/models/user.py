"""
用户表
"""
from datetime import datetime
from uuid import uuid4

from agentix.retrieval.domain.datetime_utils import utc_now

from sqlmodel import Field, SQLModel


def _uuid() -> str:
    return uuid4().hex


class User(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    username: str = Field(default="", unique=True)
    email: str = Field(index=True, unique=True)
    password_hash: str = Field(default="")
    tenant_id: str | None = Field(default=None, index=True)
    role: str = Field(default="member", index=True)
    status: str = Field(default="active", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
