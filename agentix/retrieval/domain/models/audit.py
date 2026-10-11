"""
审计日志表——高权限操作（用户/租户/kb 增删改）的审计记录。

spec §4.4-5：审计日志字段经 extra 携带操作者 tenant/user、对象、结果，
遵守结构化日志硬规则，不落敏感明文（密码等）。
"""
from datetime import datetime

from agentix.retrieval.domain.datetime_utils import utc_now
from agentix.retrieval.domain.models.user import _uuid

from sqlmodel import Field, SQLModel


class AuditLog(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    actor_user_id: str = Field(index=True)
    actor_tenant_id: str | None = Field(default=None, index=True)
    action: str = Field(index=True, description="create/update/delete")
    target_type: str = Field(index=True, description="tenant/kb/user/doc")
    target_id: str = Field(index=True)
    result: str = Field(default="success", description="success/failed")
    details: str | None = Field(default=None, description="JSON string, no sensitive data")
    created_at: datetime = Field(default_factory=utc_now, index=True)
