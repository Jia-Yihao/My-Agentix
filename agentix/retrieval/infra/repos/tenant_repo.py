"""
租户 Repo（infra 实现）
"""
from sqlmodel import Session, select

from agentix.retrieval.domain.models.tenant import Tenant


def get_tenant_by_id(session: Session, tenant_id: str) -> Tenant | None:
    return session.get(Tenant, tenant_id)


def get_tenant_by_name(session: Session, name: str) -> Tenant | None:
    stmt = select(Tenant).where(Tenant.name == name)
    return session.exec(stmt).first()


def list_tenants(
    session: Session,
    *,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Tenant], int]:
    stmt = select(Tenant).order_by(Tenant.created_at.desc())
    total = len(session.exec(select(Tenant)).all())
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    items = session.exec(stmt).all()
    return list(items), total


def create_tenant(session: Session, *, name: str) -> Tenant:
    tenant = Tenant(name=name)
    session.add(tenant)
    session.flush()
    return tenant


def update_tenant(
    session: Session,
    tenant_id: str,
    *,
    name: str | None = None,
    status: str | None = None,
) -> Tenant | None:
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        return None
    if name is not None:
        tenant.name = name
    if status is not None:
        tenant.status = status
    session.add(tenant)
    return tenant


def delete_tenant(session: Session, tenant_id: str) -> bool:
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        return False
    session.delete(tenant)
    return True
