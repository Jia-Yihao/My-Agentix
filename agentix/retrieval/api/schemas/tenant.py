"""
租户相关 Pydantic DTO（请求/响应）

只做 HTTP 层数据校验与序列化，不含 ORM 模型。
"""
from datetime import datetime
from pydantic import BaseModel, Field


class TenantCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class TenantUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    status: str | None = Field(default=None, pattern="^(active|disabled)$")


class TenantData(BaseModel):
    id: str
    name: str
    status: str
    created_at: datetime
    updated_at: datetime


class TenantListData(BaseModel):
    items: list[TenantData]
    page: int
    page_size: int
    total: int
