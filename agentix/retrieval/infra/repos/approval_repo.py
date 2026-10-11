"""
删除审批 Repo（infra 实现）

状态流转（单向）：pending → approved / rejected / timeout
- pending：删除请求已提交，等待审批人处理；
- approved：审批通过，文档/KB 进入实际删除流程；
- rejected：审批驳回，目标恢复就绪状态，删除请求终止；
- timeout：超过 DELETE_APPROVAL_TIMEOUT_SECONDS 未处理（仍允许审批人处理）。
"""
from __future__ import annotations

from sqlmodel import Session, select

from agentix.retrieval.domain.datetime_utils import utc_now
from agentix.retrieval.domain.models.approval import DeleteApproval
from agentix.retrieval.domain.models.document import Document


def find_pending_approval(
    session: Session, *, target_type: str, target_id: str
) -> DeleteApproval | None:
    stmt = select(DeleteApproval).where(
        DeleteApproval.target_type == target_type,
        DeleteApproval.target_id == target_id,
        DeleteApproval.status == "pending",
    )
    return session.exec(stmt).first()


def create_approval(
    session: Session, *, target_type: str, target_id: str, requested_by: str
) -> DeleteApproval:
    approval = DeleteApproval(
        target_type=target_type,
        target_id=target_id,
        status="pending",
        requested_by=requested_by,
    )
    session.add(approval)
    session.flush()
    session.refresh(approval)
    return approval


def get_approval(session: Session, approval_id: str) -> DeleteApproval | None:
    return session.get(DeleteApproval, approval_id)


def list_approvals(
    session: Session,
    *,
    status: str | None = None,
    requested_by: str | None = None,
    approver_scope_kb_id: str | None = None,
    limit: int = 100,
) -> list[DeleteApproval]:
    """列出审批记录。

    - status：按状态过滤；
    - requested_by：按发起人过滤（普通用户查自己发起的）；
    - approver_scope_kb_id：审批人视角，仅返回该 KB 范围内的审批
      （document 类审批取其文档所属 KB；kb 类审批取 target_id）。
    """
    stmt = select(DeleteApproval)
    if status:
        stmt = stmt.where(DeleteApproval.status == status)
    if requested_by:
        stmt = stmt.where(DeleteApproval.requested_by == requested_by)
    stmt = stmt.order_by(DeleteApproval.created_at.desc()).limit(limit)
    approvals = list(session.exec(stmt).all())
    if approver_scope_kb_id is None:
        return approvals
    scoped: list[DeleteApproval] = []
    for ap in approvals:
        if ap.target_type == "kb" and ap.target_id == approver_scope_kb_id:
            scoped.append(ap)
            continue
        if ap.target_type == "document":
            doc = session.get(Document, ap.target_id)
            if doc and doc.kb_id == approver_scope_kb_id:
                scoped.append(ap)
    return scoped


def mark_approved(
    session: Session, approval: DeleteApproval, *, approved_by: str
) -> DeleteApproval:
    approval.status = "approved"
    approval.approved_by = approved_by
    approval.processed_at = utc_now()
    session.add(approval)
    return approval


def mark_rejected(
    session: Session, approval: DeleteApproval, *, approved_by: str
) -> DeleteApproval:
    approval.status = "rejected"
    approval.approved_by = approved_by
    approval.processed_at = utc_now()
    session.add(approval)
    return approval


def pending_approval_document_ids(session: Session, kb_id: str) -> list[str]:
    """返回该 KB 内存在 pending 删除审批、对检索应不可见的文档 id 列表。

    - target_type='document'：取 target_id，且其文档属于该 kb；
    - target_type='kb' 且 target_id == kb_id：KB 整体待审批，返回该 KB 全部文档 id。
    """
    stmt = select(DeleteApproval).where(DeleteApproval.status == "pending")
    approvals = list(session.exec(stmt).all())
    excluded: list[str] = []
    kb_level_pending = False
    for ap in approvals:
        if ap.target_type == "kb" and ap.target_id == kb_id:
            kb_level_pending = True
        elif ap.target_type == "document":
            doc = session.get(Document, ap.target_id)
            if doc and doc.kb_id == kb_id:
                excluded.append(ap.target_id)
    if kb_level_pending:
        docs = list(
            session.exec(select(Document).where(Document.kb_id == kb_id)).all()
        )
        excluded.extend(d.id for d in docs)
    return excluded
