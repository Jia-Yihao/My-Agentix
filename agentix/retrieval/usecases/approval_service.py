"""
删除审批业务编排

事务边界：
- approve_approval / reject_approval：状态流转 + 副作用（文档置 deleted_pending_purge + 排队重建
  或 KB 物理删除）在同一事务 commit；
- 幂等：非 pending 态重复处理返回 APPROVAL_ALREADY_PROCESSED。
"""
from __future__ import annotations

from sqlmodel import Session

from agentix.retrieval.api.response import ErrorCode
from agentix.retrieval.api.schemas.approvals import (
    ApprovalActionData,
    ApprovalData,
    ApprovalListData,
)
from agentix.retrieval.domain.datetime_utils import utc_now
from agentix.retrieval.domain.models.approval import DeleteApproval
from agentix.retrieval.errors import AppError
from agentix.retrieval.infra.repos.approval_repo import (
    get_approval,
    list_approvals,
    mark_approved,
    mark_rejected,
)
from agentix.retrieval.infra.repos.document_repo import get_document_by_id, get_kb_file_by_id
from agentix.observability.metrics import (
    record_approval_decision,
    record_approval_timeout,
)
from agentix.retrieval.settings import settings


def _to_data(ap: DeleteApproval) -> dict:
    timed_out = (
        ap.status == "pending"
        and ap.created_at is not None
        and (utc_now() - ap.created_at).total_seconds()
        > settings.DELETE_APPROVAL_TIMEOUT_SECONDS
    )
    return ApprovalData(
        id=ap.id,
        target_type=ap.target_type,
        target_id=ap.target_id,
        status=ap.status,
        requested_by=ap.requested_by,
        approved_by=ap.approved_by,
        created_at=ap.created_at,
        processed_at=ap.processed_at,
        timed_out=timed_out,
    ).model_dump()


def list_pending_approvals(
    session: Session,
    *,
    requested_by: str | None = None,
    approver_scope_kb_id: str | None = None,
    limit: int = 100,
) -> dict:
    approvals = list_approvals(
        session,
        status="pending",
        requested_by=requested_by,
        approver_scope_kb_id=approver_scope_kb_id,
        limit=limit,
    )
    return ApprovalListData(
        items=[_to_data(a) for a in approvals], total=len(approvals)
    ).model_dump()


def approve_approval(session: Session, approval_id: str, *, approved_by: str) -> dict:
    ap = get_approval(session, approval_id)
    if not ap:
        raise AppError(
            error_code=ErrorCode.APPROVAL_NOT_FOUND,
            message="审批记录不存在",
            status_code=404,
        )
    if ap.status != "pending":
        raise AppError(
            error_code=ErrorCode.APPROVAL_ALREADY_PROCESSED,
            message="该审批已被处理，请勿重复操作",
            status_code=409,
        )

    target_gone = False
    job_id: str | None = None
    should_enqueue = False

    if ap.target_type == "document":
        doc = get_document_by_id(session, ap.target_id)
        if not doc:
            target_gone = True
        else:
            doc.status = "deleted_pending_purge"
            if doc.kb_file_id:
                kb_file = get_kb_file_by_id(session, doc.kb_file_id)
                if kb_file:
                    kb_file.status = "deleted_pending_purge"
                    session.add(kb_file)
            session.add(doc)
            from agentix.retrieval.infra.repos.index_repo import ensure_kb_index_job

            job_id, should_enqueue = ensure_kb_index_job(
                session, doc.kb_id, idempotency_key=None
            )
    elif ap.target_type == "kb":
        from agentix.retrieval.infra.repos.kb_repo import delete_kb as _delete_kb, get_kb_by_id as _get_kb

        kb = _get_kb(session, ap.target_id)
        if not kb:
            target_gone = True
        else:
            _delete_kb(session, ap.target_id)

    mark_approved(session, ap, approved_by=approved_by)
    session.commit()

    record_approval_decision(ap.target_type, "approved")
    if (
        ap.created_at is not None
        and (utc_now() - ap.created_at).total_seconds()
        > settings.DELETE_APPROVAL_TIMEOUT_SECONDS
    ):
        record_approval_timeout(ap.target_type)

    return ApprovalActionData(
        approval_id=ap.id,
        status="approved",
        target_gone=target_gone,
        job_id=job_id,
        should_enqueue=should_enqueue,
    ).model_dump()


def reject_approval(session: Session, approval_id: str, *, approved_by: str) -> dict:
    ap = get_approval(session, approval_id)
    if not ap:
        raise AppError(
            error_code=ErrorCode.APPROVAL_NOT_FOUND,
            message="审批记录不存在",
            status_code=404,
        )
    if ap.status != "pending":
        raise AppError(
            error_code=ErrorCode.APPROVAL_ALREADY_PROCESSED,
            message="该审批已被处理，请勿重复操作",
            status_code=409,
        )

    # 驳回：文档/KB 在待审批期间未被改动状态，无需恢复动作
    mark_rejected(session, ap, approved_by=approved_by)
    session.commit()

    record_approval_decision(ap.target_type, "rejected")
    if (
        ap.created_at is not None
        and (utc_now() - ap.created_at).total_seconds()
        > settings.DELETE_APPROVAL_TIMEOUT_SECONDS
    ):
        record_approval_timeout(ap.target_type)

    return ApprovalActionData(
        approval_id=ap.id,
        status="rejected",
    ).model_dump()


def get_approval_detail(session: Session, approval_id: str) -> dict:
    ap = get_approval(session, approval_id)
    if not ap:
        raise AppError(
            error_code=ErrorCode.APPROVAL_NOT_FOUND,
            message="审批记录不存在",
            status_code=404,
        )
    return _to_data(ap)
