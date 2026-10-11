"""
Database model tracking tenants — the top-level isolation boundary.

Users and knowledge bases both belong to a tenant. tenant_id is globally
unique and immutable once created (spec §6.1).
"""
from sqlalchemy import Column, DateTime, String, func

from agentix.infrastructure.core.database import Base


class Tenant(Base):
    """Tenant model — highest isolation layer (spec §5.5.1 / §6.1)."""

    __tablename__ = "tenants"

    id = Column(String(64), primary_key=True, index=True)
    name = Column(String(128), nullable=False, index=True)
    status = Column(String(20), default="active", index=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<Tenant id={self.id} name={self.name!r}>"
