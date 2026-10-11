"""
文档变更操作（删除 / 替换上传 / 改名）

从 document_service.py 拆出（按域拆分批次）。

事务边界：
- delete_document：软删标记 + ensure_kb_index_job 后统一 commit；
- _handle_replace：commit 前所有变更在同一事务；幂等命中时 rollback；
  物理对象删除在 commit 之后尽力执行（失败不影响已提交事务）。
- update_document_title：单事务 commit。
"""
import logging
import os
from typing import Optional

from sqlmodel import Session

from agentix.retrieval.api.response import ErrorCode
from agentix.retrieval.api.schemas.documents import DocumentData, DocumentDeleteData, UploadData
from agentix.retrieval.domain.datetime_utils import utc_now
from agentix.retrieval.domain.models.document import Document, FileObject
from agentix.retrieval.errors import AppError
from agentix.retrieval.infra.repos.approval_repo import find_pending_approval, create_approval
from agentix.retrieval.infra.repos.document_repo import (
    delete_chunks_by_document_id,
    find_document,
    get_document_by_id,
    get_kb_file_by_id,
    unlink_file_object_if_unused,
)

from agentix.retrieval.infra.repos.index_repo import (
    ensure_kb_index_job,
    get_index_job_by_idempotency_key,
    get_latest_index_job_for_kb,
)
from agentix.retrieval.infra.storage.s3_minio import delete_object
from agentix.retrieval.settings import settings

logger = logging.getLogger(__name__)


def _to_doc_data(doc: Document) -> dict:
    return DocumentData(
        id=doc.id,
        kb_id=doc.kb_id,
        kb_file_id=doc.kb_file_id,
        title=doc.title,
        status=doc.status,
        version=doc.version,
        folder_id=doc.folder_id,
        folder_path=doc.folder_path,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        has_thumbnail=bool((doc.thumbnail_key or "").strip()),
    ).model_dump()


def delete_document(session: Session, document_id: str, *, requested_by: str | None = None) -> dict:
    """标记文档待清理；索引成功后再物理清理，避免产生孤儿向量。

    审批启用时：进入待审批态（不立即标记 deleted_pending_purge、不排队重建），
    对同一文档未决审批去重（幂等）。
    """
    doc = get_document_by_id(session, document_id)
    if not doc:
        return DocumentDeleteData(
            status="deleted",
            document_id=document_id,
            physical_file_deleted=False,
            job_id=None,
            should_enqueue=False,
        ).model_dump()

    kb_id = doc.kb_id

    if settings.DELETE_APPROVAL_ENABLED:
        return _request_delete_approval(session, doc, requested_by=requested_by)

    doc.status = "deleted_pending_purge"
    if doc.kb_file_id:
        kb_file = get_kb_file_by_id(session, doc.kb_file_id)
        if kb_file:
            kb_file.status = "deleted_pending_purge"
            session.add(kb_file)
    session.add(doc)
    job_id, should_enqueue = ensure_kb_index_job(session, kb_id, idempotency_key=None)
    session.commit()

    return DocumentDeleteData(
        status="deleted_pending_purge",
        document_id=document_id,
        physical_file_deleted=False,
        job_id=job_id,
        should_enqueue=should_enqueue,
    ).model_dump()


def _request_delete_approval(
    session: Session, doc: Document, *, requested_by: str | None
) -> dict:
    """审批启用时的删除请求：进入待审批态（幂等复用未决审批）。"""
    existing = find_pending_approval(session, target_type="document", target_id=doc.id)
    if existing is None:
        existing = create_approval(
            session,
            target_type="document",
            target_id=doc.id,
            requested_by=requested_by or "",
        )
        session.commit()
        from agentix.observability.metrics import record_approval_created

        record_approval_created("document")
    return DocumentDeleteData(
        status="delete_pending_approval",
        document_id=doc.id,
        physical_file_deleted=False,
        job_id=None,
        should_enqueue=False,
        approval_id=existing.id,
        approval_status=existing.status,
    ).model_dump()


def _handle_replace(
    session: Session,
    kb_id: str,
    rid: str,
    doc_title: str,
    file_object: FileObject,
    normalized_filename: str,
    size: int,
    sha256: str,
    idempotency_key: Optional[str],
    auto_renamed: bool,
) -> dict:
    """替换模式处理逻辑"""
    document = get_document_by_id(session, rid)
    if not document or document.kb_id != kb_id:
        raise AppError(error_code=ErrorCode.DOCUMENT_NOT_FOUND, message="文档不存在或已被删除", status_code=404)
    if not document.kb_file_id:
        raise AppError(error_code=ErrorCode.VALIDATION_ERROR, message="该文档无文件记录，无法替换上传", status_code=400)

    kb_file = get_kb_file_by_id(session, document.kb_file_id)
    if not kb_file:
        raise AppError(error_code=ErrorCode.VALIDATION_ERROR, message="文件记录不存在", status_code=400)

    old_fo_id = kb_file.file_object_id

    # 替换为同一 FileObject（内容完全相同、去重命中）：不删 chunk、不降级 draft，避免无索引进队时长期停在 draft
    if file_object.id == old_fo_id:
        if normalized_filename != (kb_file.original_filename or ""):
            kb_file.original_filename = normalized_filename
            kb_file.updated_at = utc_now()
            session.add(kb_file)
        latest = get_latest_index_job_for_kb(session, kb_id)
        if latest:
            job_id, should_enqueue = latest.id, False
        else:
            job_id, should_enqueue = ensure_kb_index_job(session, kb_id, idempotency_key=None)
        session.commit()
        session.refresh(document)
        return UploadData(
            document_id=document.id,
            job_id=job_id,
            original_filename=normalized_filename,
            size_bytes=size,
            sha256=sha256,
            is_duplicate=True,
            is_replace=True,
            auto_renamed=auto_renamed,
            should_enqueue=should_enqueue,
        ).model_dump()

    delete_chunks_by_document_id(session, document.id)

    kb_file.file_object_id = file_object.id
    kb_file.original_filename = normalized_filename
    kb_file.updated_at = utc_now()
    session.add(kb_file)

    physical_drop = unlink_file_object_if_unused(session, old_fo_id)

    document.version += 1
    document.status = "draft"
    document.updated_at = utc_now()
    session.add(document)

    job_key = idempotency_key or f"{document.id}:v{document.version}"
    existing_job = get_index_job_by_idempotency_key(session, job_key)
    if existing_job:
        session.rollback()
        return UploadData(
            document_id=document.id,
            job_id=existing_job.id,
            original_filename=normalized_filename,
            size_bytes=size,
            sha256=sha256,
            is_duplicate=True,
            is_replace=True,
            auto_renamed=auto_renamed,
            should_enqueue=False,
        ).model_dump()

    job_id, should_enqueue = ensure_kb_index_job(session, kb_id, idempotency_key=job_key)
    session.commit()
    session.refresh(document)

    if physical_drop:
        try:
            delete_object(physical_drop)
        except Exception:
            # 「替换文档」会把旧对象从对象存储里删掉；失败则留下一个**无人引用
            # 也无人知晓**的孤儿对象（存储只涨不跌，且没有任何指标能看出来）。
            # 同层 thumbnail_service 对同一操作是记 warning 的 —— 此处此前是
            # `except Exception: pass`，属同类操作两套标准（见 §7.14）。
            # 事务已 commit，删除失败**不能**回滚，因此只记日志、不重抛。
            logger.warning(
                "failed to physically drop replaced object: key=%s", physical_drop, exc_info=True
            )

    return UploadData(
        document_id=document.id,
        job_id=job_id,
        original_filename=normalized_filename,
        size_bytes=size,
        sha256=sha256,
        is_duplicate=False,
        is_replace=True,
        auto_renamed=auto_renamed,
        should_enqueue=should_enqueue,
    ).model_dump()


def update_document_title(session: Session, *, document_id: str, title: str) -> dict:
    clean = (title or "").strip()
    if not clean:
        raise AppError(error_code=ErrorCode.VALIDATION_ERROR, message="文档名称不能为空", status_code=400)
    if "/" in clean or "\\" in clean:
        raise AppError(error_code=ErrorCode.VALIDATION_ERROR, message='文档名称不能包含 "/"', status_code=400)

    doc = get_document_by_id(session, document_id)
    if not doc:
        raise AppError(error_code=ErrorCode.DOCUMENT_NOT_FOUND, message="文档不存在或已被删除", status_code=404)

    base_title = os.path.splitext(clean)[0] if clean.lower().endswith(".pdf") else clean
    conflict = find_document(
        session,
        doc.kb_id,
        doc.folder_path,
        base_title,
        folder_id=doc.folder_id,
    )
    if conflict and conflict.id != doc.id:
        raise AppError(
            error_code=ErrorCode.NAME_CONFLICT,
            message="该位置已存在同名文档",
            status_code=409,
            details={"existing_document_id": conflict.id},
        )

    doc.title = base_title
    doc.updated_at = utc_now()
    if doc.kb_file_id:
        kb_file = get_kb_file_by_id(session, doc.kb_file_id)
        if kb_file:
            ext = ".pdf"
            if kb_file.original_filename and "." in kb_file.original_filename:
                ext = os.path.splitext(kb_file.original_filename)[1] or ".pdf"
            kb_file.original_filename = f"{base_title}{ext}"
            kb_file.updated_at = utc_now()
            session.add(kb_file)
    session.add(doc)
    session.commit()
    session.refresh(doc)
    return _to_doc_data(doc)
