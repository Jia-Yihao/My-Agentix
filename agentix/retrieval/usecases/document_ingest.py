"""
文档入库管线（批量上传 / 单条 ingest）

从 document_service.py 拆出（按域拆分批次）。

事务边界：
- _ingest_one_pdf_create_no_commit：在调用方 session 内落库，**不 commit**；
- commit_documents_batch：整批同一事务，最后 commit；任何异常统一 rollback。
"""
import os
import uuid
from typing import Optional

from sqlmodel import Session

from agentix.retrieval.api.response import ErrorCode
from agentix.retrieval.domain.datetime_utils import utc_now
from agentix.retrieval.domain.models.document import Document, FileObject, KbFile
from agentix.retrieval.errors import AppError
from agentix.retrieval.infra.repos.document_repo import find_document, get_file_object_by_sha256
from agentix.retrieval.infra.repos.index_repo import ensure_kb_index_job
from agentix.retrieval.infra.storage.local_fs import storage
from agentix.retrieval.infra.storage.s3_minio import upload_file
from agentix.retrieval.usecases.document_upload_helpers import (
    _build_object_key,
    _ensure_file_object_uploaded,
    _folder_and_filename_from_relative,
    _object_needs_reupload,
    _upload_content_type,
    normalize_folder_path,
)


def _resolve_normalized_title(
    session: Session,
    *,
    kb_id: str,
    folder_path: Optional[str],
    folder_id: Optional[str],
    raw_filename: str,
    on_conflict: str,
) -> tuple[str, bool]:
    """
    计算 normalized 文件名并执行冲突策略（create/auto_rename 共用）。

    返回 (normalized_filename, auto_renamed)：
    - auto_rename：同名时追加时间戳重试（最多 100 次），失败抛 VALIDATION_ERROR；
    - create：同名抛 NAME_CONFLICT(409)。
    """
    normalized = raw_filename if raw_filename.lower().endswith(".pdf") else f"{raw_filename}.pdf"
    auto_renamed = False

    if on_conflict == "auto_rename":
        for _ in range(100):
            doc_title = os.path.splitext(normalized)[0]
            if not find_document(session, kb_id, folder_path, doc_title, folder_id=folder_id):
                break
            base, ext = os.path.splitext(normalized)
            ts = utc_now().strftime("%Y%m%d%H%M%S")
            normalized = f"{base}_{ts}{ext}"
            auto_renamed = True
        else:
            raise AppError(error_code=ErrorCode.VALIDATION_ERROR, message="无法生成唯一文件名", status_code=400)

    if on_conflict == "create":
        existing = find_document(
            session, kb_id, folder_path, os.path.splitext(normalized)[0], folder_id=folder_id
        )
        if existing:
            raise AppError(
                error_code=ErrorCode.NAME_CONFLICT,
                message="该位置已存在同名文档",
                status_code=409,
                details={
                    "existing_document_id": existing.id,
                    "existing_title": existing.title,
                    "incoming_filename": raw_filename,
                },
            )

    return normalized, auto_renamed


async def _ingest_one_pdf_create_no_commit(
    session: Session,
    file: object,
    raw_filename: str,
    kb_id: str,
    user_id: str,
    folder_path: Optional[str],
    on_conflict: str,
    folder_id: Optional[str] = None,
) -> dict:
    """
    在已有 session 内走 create/auto_rename 落库一条文档，不 commit、不创建 IndexJob。
    """
    from agentix.retrieval.usecases.folder_service import resolve_folder_for_document

    resolved_id, resolved_path = resolve_folder_for_document(
        session, kb_id, folder_id=folder_id, folder_path=folder_path
    )
    folder_id = resolved_id
    folder_path = resolved_path

    normalized, auto_renamed = _resolve_normalized_title(
        session,
        kb_id=kb_id,
        folder_path=folder_path,
        folder_id=folder_id,
        raw_filename=raw_filename,
        on_conflict=on_conflict,
    )

    temp_path, sha256_hash, size = await storage.save_upload_to_temp_and_hash(file)
    is_duplicate = False

    try:
        existing_file = get_file_object_by_sha256(session, sha256_hash)

        if existing_file:
            file_object = existing_file
            is_duplicate = True

            if _object_needs_reupload(object_key=existing_file.object_key, size_bytes=existing_file.size_bytes):
                _ensure_file_object_uploaded(
                    session=session,
                    file_object=existing_file,
                    temp_path=temp_path,
                    content_type=_upload_content_type(file),
                    user_id=user_id,
                    kb_id=kb_id,
                    raw_filename=raw_filename,
                    size_bytes=size,
                )
                temp_path = ""
        else:
            file_object = FileObject(
                id=uuid.uuid4().hex,
                storage_provider="minio",
                object_key="",
                sha256=sha256_hash,
                size_bytes=size,
                file_type="pdf",
            )
            object_key = _build_object_key(
                user_id=user_id,
                kb_id=kb_id,
                file_object_id=file_object.id,
                raw_filename=raw_filename,
            )
            upload_file(temp_path, object_key, content_type=_upload_content_type(file))
            file_object.object_key = object_key
            temp_path = ""
            session.add(file_object)

        kb_file = KbFile(
            kb_id=kb_id,
            file_object_id=file_object.id,
            original_filename=raw_filename,
            uploaded_by=user_id,
            status="active",
        )
        session.add(kb_file)

        document = Document(
            kb_id=kb_id,
            kb_file_id=kb_file.id,
            title=os.path.splitext(normalized)[0],
            status="draft",
            version=1,
            folder_id=folder_id,
            folder_path=folder_path,
        )
        session.add(document)
        session.flush()

        return {
            "document_id": document.id,
            "original_filename": raw_filename,
            "size_bytes": size,
            "sha256": sha256_hash,
            "is_duplicate": is_duplicate,
            "auto_renamed": auto_renamed,
            "folder_id": folder_id,
            "folder_path": folder_path,
            "title": document.title,
        }
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


async def commit_documents_batch(
    session: Session,
    *,
    kb_id: str,
    user_id: str,
    entries: list[tuple[object, Optional[str], str]],
    on_conflict: str = "create",
    idempotency_key: Optional[str] = None,
    folder_id: Optional[str] = None,
) -> dict:
    """
    批量上传 PDF：同一事务内写入多条 Document，最后只创建/复用一条 KB 级 IndexJob 并 commit。
    entries: (UploadFile, folder_path 已归一化, raw_filename 展示名)
    """
    batch_max = 100
    if not entries:
        raise AppError(error_code=ErrorCode.VALIDATION_ERROR, message="至少上传一个 PDF 文件", status_code=400)
    if len(entries) > batch_max:
        raise AppError(
            error_code=ErrorCode.VALIDATION_ERROR,
            message=f"单次最多上传 {batch_max} 个文件",
            status_code=400,
        )
    if on_conflict not in {"create", "auto_rename"}:
        raise AppError(
            error_code=ErrorCode.VALIDATION_ERROR,
            message="批量上传仅支持 on_conflict=create 或 auto_rename",
            status_code=400,
        )

    items: list[dict] = []
    try:
        for file, fp, raw_fn in entries:
            row = await _ingest_one_pdf_create_no_commit(
                session, file, raw_fn, kb_id, user_id, fp, on_conflict, folder_id=folder_id
            )
            items.append(row)

        job_id, should_enqueue = ensure_kb_index_job(session, kb_id, idempotency_key=idempotency_key)
        session.commit()
    except AppError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise

    return {
        "kb_id": kb_id,
        "job_id": job_id,
        "should_enqueue": should_enqueue,
        "items": items,
        "total": len(items),
    }


def build_batch_upload_entries(
    files: list[object],
    folder_path: Optional[str],
    relative_paths: Optional[list[str]],
) -> list[tuple[object, Optional[str], str]]:
    """构造 (file, folder_path, raw_filename) 列表并校验 PDF / 路径数量。"""
    if not files:
        raise AppError(error_code=ErrorCode.VALIDATION_ERROR, message="至少上传一个 PDF 文件", status_code=400)
    if relative_paths is not None and len(relative_paths) != len(files):
        raise AppError(
            error_code=ErrorCode.VALIDATION_ERROR,
            message="relative_paths 数组长度必须与文件数量一致",
            status_code=400,
        )
    base_fp = normalize_folder_path(folder_path)
    entries: list[tuple[object, Optional[str], str]] = []
    for i, file in enumerate(files):
        raw_stripped = (file.filename or "").strip()
        if relative_paths:
            fp_i, raw_fn = _folder_and_filename_from_relative(relative_paths[i])
        else:
            fp_i, raw_fn = base_fp, raw_stripped
        if not raw_fn:
            raise AppError(error_code=ErrorCode.VALIDATION_ERROR, message="文件名不能为空", status_code=400)
        is_pdf = raw_fn.lower().endswith(".pdf") or (file.content_type or "").lower() in {
            "application/pdf",
            "application/x-pdf",
        }
        if not is_pdf:
            raise AppError(error_code=ErrorCode.VALIDATION_ERROR, message="仅支持 PDF 文件", status_code=400)
        entries.append((file, fp_i, raw_fn if relative_paths else raw_stripped))
    return entries
