"""
资源归属校验：区分「不存在」404 与 「存在但无权」403。

R2.4 升级为租户层校验：tenant → kb → doc/session 逐层链。
旧函数保留为兼容包装，委托到新函数（按 user_id 查 tenant_id）。
"""
from sqlmodel import Session

from agentix.retrieval.domain.models.document import Document
from agentix.retrieval.domain.models.index import IndexJob
from agentix.retrieval.domain.models.kb import KnowledgeBase
from agentix.retrieval.domain.models.user import User
from agentix.retrieval.domain.models.chat import Conversation
from agentix.retrieval.infra.repos.kb_repo import get_kb_by_id
from agentix.retrieval.errors import AppError
from agentix.retrieval.api.response import ErrorCode, get_status_code


def require_kb_in_tenant(session: Session, kb_id: str, tenant_id: str | None) -> KnowledgeBase:
    """校验 kb 存在且属于指定租户。

    - kb 不存在 → 404（KB_NOT_FOUND）
    - kb.tenant_id 不匹配 → 403（FORBIDDEN，不泄露存在性）
    """
    kb = get_kb_by_id(session, kb_id)
    if not kb:
        raise AppError(
            error_code=ErrorCode.KB_NOT_FOUND,
            message="知识库不存在",
            status_code=get_status_code(ErrorCode.KB_NOT_FOUND),
        )
    if kb.tenant_id is None or kb.tenant_id != tenant_id:
        raise AppError(
            error_code=ErrorCode.FORBIDDEN,
            message="无权访问该知识库",
            status_code=get_status_code(ErrorCode.FORBIDDEN),
        )
    return kb


def require_doc_in_tenant(session: Session, document_id: str, tenant_id: str | None) -> Document:
    """校验 doc 存在且其所属 kb 属于指定租户。"""
    doc = session.get(Document, document_id)
    if not doc:
        raise AppError(
            error_code=ErrorCode.DOCUMENT_NOT_FOUND,
            message="文档不存在",
            status_code=get_status_code(ErrorCode.DOCUMENT_NOT_FOUND),
        )
    require_kb_in_tenant(session, doc.kb_id, tenant_id)
    return doc


def require_session_in_tenant(session: Session, conversation_id: str, tenant_id: str | None) -> Conversation:
    """校验会话存在且其所属 kb 属于指定租户。"""
    conv = session.get(Conversation, conversation_id)
    if not conv:
        raise AppError(
            error_code=ErrorCode.CONVERSATION_NOT_FOUND,
            message="会话不存在",
            status_code=get_status_code(ErrorCode.CONVERSATION_NOT_FOUND),
        )
    require_kb_in_tenant(session, conv.kb_id, tenant_id)
    return conv


def require_index_job_in_tenant(session: Session, job_id: str, tenant_id: str | None) -> IndexJob:
    """校验索引任务存在且其所属 kb 属于指定租户。"""
    job = session.get(IndexJob, job_id)
    if not job:
        raise AppError(
            error_code=ErrorCode.JOB_NOT_FOUND,
            message="index job not found",
            status_code=get_status_code(ErrorCode.JOB_NOT_FOUND),
            details={"job_id": job_id},
        )
    require_kb_in_tenant(session, job.kb_id, tenant_id)
    return job


def _resolve_tenant_id(session: Session, user_id: str) -> str | None:
    """从 user_id 查 tenant_id（兼容旧调用方）。"""
    user = session.get(User, user_id)
    return user.tenant_id if user else None


def require_kb_owned(session: Session, kb_id: str, user_id: str) -> KnowledgeBase:
    """兼容包装：按 user_id 查 tenant_id 后委托到 require_kb_in_tenant。"""
    tenant_id = _resolve_tenant_id(session, user_id)
    return require_kb_in_tenant(session, kb_id, tenant_id)


def require_document_readable(session: Session, document_id: str, user_id: str) -> Document:
    """兼容包装：按 user_id 查 tenant_id 后委托到 require_doc_in_tenant。"""
    tenant_id = _resolve_tenant_id(session, user_id)
    return require_doc_in_tenant(session, document_id, tenant_id)


def require_index_job_readable(session: Session, job_id: str, user_id: str) -> IndexJob:
    """兼容包装：按 user_id 查 tenant_id 后委托到 require_index_job_in_tenant。"""
    tenant_id = _resolve_tenant_id(session, user_id)
    return require_index_job_in_tenant(session, job_id, tenant_id)
