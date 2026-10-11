"""
知识库业务编排
"""
import logging

from sqlmodel import Session

from agentix.retrieval.domain.models.kb import KnowledgeBase
from agentix.retrieval.api.schemas.kb import KbData, KbListData
from agentix.retrieval.settings import settings

logger = logging.getLogger("agentix.retrieval.kb")


def _create_kb_impl(
    session: Session,
    *,
    name: str,
    description: str | None = None,
    owner_user_id: str,
    tenant_id: str | None = None,
) -> dict:
    kb = KnowledgeBase(
        name=name,
        description=description,
        owner_user_id=owner_user_id,
        tenant_id=tenant_id,
    )
    session.add(kb)
    session.commit()
    session.refresh(kb)
    return _to_data(kb)


def _get_kb_by_id_impl(session: Session, kb_id: str) -> dict | None:
    from agentix.retrieval.infra.repos.kb_repo import get_kb_by_id as _get
    kb = _get(session, kb_id)
    return _to_data(kb) if kb else None


def _list_kbs_impl(
    session: Session,
    owner_user_id: str | None = None,
    page: int = 1,
    page_size: int = 20,
    tenant_id: str | None = None,
) -> dict:
    from agentix.retrieval.infra.repos.kb_repo import list_kbs as _list
    items, total = _list(session, owner_user_id, page, page_size, tenant_id=tenant_id)
    return KbListData(
        items=[KbData(**_to_data(kb)) for kb in items],
        page=page,
        page_size=page_size,
        total=total,
    ).model_dump()


def _remove_kb_impl(session: Session, kb_id: str, *, requested_by: str | None = None) -> dict | None:
    """删除知识库。

    审批启用时：进入待审批态（不立即物理删除），幂等复用未决审批；
    审批停用时：直接物理删除（与现状一致）。

    Returns:
        {"status": "deleted"|"delete_pending_approval", "kb_id": ..., ...} 或 None（KB 不存在）。
    """
    from agentix.retrieval.infra.repos.kb_repo import get_kb_by_id as _get
    kb = _get(session, kb_id)
    if not kb:
        return None

    if settings.DELETE_APPROVAL_ENABLED:
        from agentix.retrieval.infra.repos.approval_repo import find_pending_approval, create_approval

        existing = find_pending_approval(session, target_type="kb", target_id=kb_id)
        if existing is None:
            existing = create_approval(
                session,
                target_type="kb",
                target_id=kb_id,
                requested_by=requested_by or "",
            )
            session.commit()
            from agentix.observability.metrics import record_approval_created

            record_approval_created("kb")
        return {
            "status": "delete_pending_approval",
            "kb_id": kb_id,
            "approval_id": existing.id,
            "approval_status": existing.status,
        }

    from agentix.retrieval.infra.repos.kb_repo import delete_kb as _delete
    ok = _delete(session, kb_id)
    if ok:
        session.commit()
    return {"status": "deleted", "kb_id": kb_id} if ok else None


def _update_kb_impl(
    session: Session,
    kb_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
) -> dict | None:
    from agentix.retrieval.infra.repos.kb_repo import update_kb as _update
    kb = _update(session, kb_id, name=name, description=description)
    if not kb:
        return None
    session.commit()
    session.refresh(kb)
    return _to_data(kb)


def _to_data(kb: KnowledgeBase) -> dict:
    return KbData(
        id=kb.id,
        name=kb.name,
        description=kb.description,
        owner_user_id=kb.owner_user_id,
        tenant_id=kb.tenant_id,
        created_at=kb.created_at,
        updated_at=kb.updated_at,
    ).model_dump()


class KbService:
    def __init__(self, session: Session):
        self.session = session

    def create_kb(
        self,
        *,
        name: str,
        description: str | None = None,
        owner_user_id: str,
        tenant_id: str | None = None,
        actor_user_id: str | None = None,
        actor_tenant_id: str | None = None,
    ) -> dict:
        result = _create_kb_impl(
            self.session,
            name=name,
            description=description,
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
        )
        if actor_user_id:
            from agentix.retrieval.usecases.audit_service import write_audit
            write_audit(
                self.session,
                actor_user_id=actor_user_id,
                actor_tenant_id=actor_tenant_id,
                action="create",
                target_type="kb",
                target_id=result["id"],
                details={"name": name},
            )
            self.session.commit()
        return result

    def get_kb_by_id(self, *, kb_id: str) -> dict | None:
        return _get_kb_by_id_impl(self.session, kb_id)

    def list_kbs(
        self,
        *,
        owner_user_id: str | None = None,
        page: int = 1,
        page_size: int = 20,
        tenant_id: str | None = None,
    ) -> dict:
        return _list_kbs_impl(self.session, owner_user_id, page, page_size, tenant_id=tenant_id)

    def remove_kb(self, *, kb_id: str, requested_by: str | None = None) -> dict | None:
        return _remove_kb_impl(self.session, kb_id, requested_by=requested_by)

    def update_kb(
        self,
        *,
        kb_id: str,
        name: str | None = None,
        description: str | None = None,
    ) -> dict | None:
        return _update_kb_impl(self.session, kb_id, name=name, description=description)
