"""
租户业务编排——Tenant CRUD usecase。

管理员可增删改查租户（spec §5.5.1 / §6.1）。
tenant_id 全局唯一、创建后不可变；name ≤128 且租户内唯一。
高权限操作写审计日志（spec §4.4-5）。
"""
import logging

from sqlmodel import Session

from agentix.retrieval.domain.models.tenant import Tenant
from agentix.retrieval.api.schemas.tenant import TenantData, TenantListData
from agentix.retrieval.api.response import ErrorCode
from agentix.retrieval.errors import AppError
from agentix.retrieval.usecases.audit_service import write_audit

logger = logging.getLogger("agentix.retrieval.tenant")


def _to_data(tenant: Tenant) -> dict:
    return TenantData(
        id=tenant.id,
        name=tenant.name,
        status=tenant.status,
        created_at=tenant.created_at,
        updated_at=tenant.updated_at,
    ).model_dump()


def _create_tenant_impl(session: Session, *, name: str) -> dict:
    from agentix.retrieval.infra.repos.tenant_repo import get_tenant_by_name, create_tenant

    existing = get_tenant_by_name(session, name)
    if existing:
        raise AppError(
            error_code=ErrorCode.TENANT_NAME_CONFLICT,
            message=f"租户名称已存在: {name}",
            status_code=409,
        )
    tenant = create_tenant(session, name=name)
    session.commit()
    session.refresh(tenant)
    logger.info("tenant.created", extra={"tenant_id": tenant.id, "tenant_name": name})
    return _to_data(tenant)


def _get_tenant_impl(session: Session, tenant_id: str) -> dict | None:
    from agentix.retrieval.infra.repos.tenant_repo import get_tenant_by_id

    tenant = get_tenant_by_id(session, tenant_id)
    return _to_data(tenant) if tenant else None


def _list_tenants_impl(session: Session, *, page: int = 1, page_size: int = 20) -> dict:
    from agentix.retrieval.infra.repos.tenant_repo import list_tenants

    items, total = list_tenants(session, page=page, page_size=page_size)
    return TenantListData(
        items=[TenantData(**_to_data(t)) for t in items],
        page=page,
        page_size=page_size,
        total=total,
    ).model_dump()


def _update_tenant_impl(
    session: Session,
    tenant_id: str,
    *,
    name: str | None = None,
    status: str | None = None,
) -> dict | None:
    from agentix.retrieval.infra.repos.tenant_repo import get_tenant_by_id, get_tenant_by_name, update_tenant

    tenant = get_tenant_by_id(session, tenant_id)
    if not tenant:
        return None

    if name is not None and name != tenant.name:
        conflict = get_tenant_by_name(session, name)
        if conflict:
            raise AppError(
                error_code=ErrorCode.TENANT_NAME_CONFLICT,
                message=f"租户名称已存在: {name}",
                status_code=409,
            )

    updated = update_tenant(session, tenant_id, name=name, status=status)
    if not updated:
        return None
    session.commit()
    session.refresh(updated)
    logger.info("tenant.updated", extra={"tenant_id": tenant_id})
    return _to_data(updated)


def _delete_tenant_impl(session: Session, tenant_id: str) -> dict | None:
    from agentix.retrieval.infra.repos.tenant_repo import get_tenant_by_id, delete_tenant

    tenant = get_tenant_by_id(session, tenant_id)
    if not tenant:
        return None
    delete_tenant(session, tenant_id)
    session.commit()
    logger.info("tenant.deleted", extra={"tenant_id": tenant_id})
    return {"status": "deleted", "tenant_id": tenant_id}


class TenantService:
    def __init__(self, session: Session):
        self.session = session

    def create_tenant(
        self,
        *,
        name: str,
        actor_user_id: str | None = None,
        actor_tenant_id: str | None = None,
    ) -> dict:
        result = _create_tenant_impl(self.session, name=name)
        if actor_user_id:
            write_audit(
                self.session,
                actor_user_id=actor_user_id,
                actor_tenant_id=actor_tenant_id,
                action="create",
                target_type="tenant",
                target_id=result["id"],
                details={"name": name},
            )
            self.session.commit()
        return result

    def get_tenant(self, *, tenant_id: str) -> dict | None:
        return _get_tenant_impl(self.session, tenant_id)

    def list_tenants(self, *, page: int = 1, page_size: int = 20) -> dict:
        return _list_tenants_impl(self.session, page=page, page_size=page_size)

    def update_tenant(
        self,
        *,
        tenant_id: str,
        name: str | None = None,
        status: str | None = None,
        actor_user_id: str | None = None,
        actor_tenant_id: str | None = None,
    ) -> dict | None:
        result = _update_tenant_impl(self.session, tenant_id, name=name, status=status)
        if result is not None and actor_user_id:
            write_audit(
                self.session,
                actor_user_id=actor_user_id,
                actor_tenant_id=actor_tenant_id,
                action="update",
                target_type="tenant",
                target_id=tenant_id,
                details={"name": name, "status": status},
            )
            self.session.commit()
        return result

    def delete_tenant(
        self,
        *,
        tenant_id: str,
        actor_user_id: str | None = None,
        actor_tenant_id: str | None = None,
    ) -> dict | None:
        result = _delete_tenant_impl(self.session, tenant_id)
        if result is not None and actor_user_id:
            write_audit(
                self.session,
                actor_user_id=actor_user_id,
                actor_tenant_id=actor_tenant_id,
                action="delete",
                target_type="tenant",
                target_id=tenant_id,
            )
            self.session.commit()
        return result
