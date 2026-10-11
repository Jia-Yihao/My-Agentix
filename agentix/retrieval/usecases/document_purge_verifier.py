"""文档删除后置校验（R3.3）。

删除链路（置 deleted_pending_purge -> 索引重建 -> 物理清理）执行完后，
确认该文档不再留下任何可被检索的痕迹：chunk 记录、活动 manifest、文档记录本身。
"""
from __future__ import annotations

import logging

from sqlmodel import Session

from agentix.retrieval.infra.repos.document_repo import (
    count_chunks_by_document,
    get_document_by_id,
    manifest_has_document,
)
from agentix.observability.metrics import record_purge_verify_failed

logger = logging.getLogger(__name__)


def verify_document_purged(
    session: Session,
    *,
    kb_id: str,
    document_id: str,
) -> tuple[bool, str]:
    """返回 (ok, reason)。ok=True 时 reason 为空串。

    失败原因取值：``chunks_remaining:<n>`` / ``manifest_remaining`` / ``document_remaining:<status>``。
    """
    reason = _remaining_reason(session, document_id)
    if not reason:
        return True, ""
    record_purge_verify_failed(kb_id)
    logger.error(
        "purge verify failed",
        extra={
            "stage": "purge.verify",
            "kb_id": kb_id,
            "doc_id": document_id,
            "status": "failed",
            "reason": reason,
        },
    )
    return False, reason


def _remaining_reason(session: Session, document_id: str) -> str:
    """按「可被召回的可能性」从高到低检查，返回首个残留原因；无残留返回空串。"""
    n_chunks = count_chunks_by_document(session, document_id)
    if n_chunks:
        return f"chunks_remaining:{n_chunks}"
    if manifest_has_document(session, document_id):
        return "manifest_remaining"
    doc = get_document_by_id(session, document_id)
    if doc is not None:
        return f"document_remaining:{doc.status}"
    return ""
