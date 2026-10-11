"""
文档上传主流程与 DocumentService 门面

按域拆分（2026-09-07 批次）后的门面模块：
- 上传辅助函数：agentix.retrieval.usecases.document_upload_helpers（路径/object key/补传）
- 批量入库管线：agentix.retrieval.usecases.document_ingest（ingest + 批量事务）
- 删除/替换/改名：agentix.retrieval.usecases.document_mutation

对外导入保持稳定：DocumentService / UploadCommand / normalize_folder_path。

核心逻辑：文件落盘 -> 去重 -> 创建 Document/KbFile -> 合并为 KB 级 IndexJob -> 按需触发后台索引。
事务边界：upload_document 正常路径统一 commit，幂等命中 rollback；临时文件 finally 清理。
"""
import os
import uuid
from typing import Optional

from sqlmodel import Session

from agentix.retrieval.api.response import ErrorCode
from agentix.retrieval.api.schemas.documents import UploadData
from agentix.retrieval.domain.models.document import Document, FileObject, KbFile
from agentix.retrieval.errors import AppError
from agentix.retrieval.infra.repos.document_repo import get_file_object_by_id, get_file_object_by_sha256, get_kb_file_by_id
from agentix.retrieval.infra.repos.index_repo import ensure_kb_index_job, get_index_job_by_idempotency_key
from agentix.retrieval.infra.storage.local_fs import storage
from agentix.retrieval.infra.storage.s3_minio import upload_file
from agentix.retrieval.usecases.document_ingest import (
    _resolve_normalized_title,
    build_batch_upload_entries,
    commit_documents_batch,
)
from agentix.retrieval.usecases.document_mutation import (
    _handle_replace,
    _to_doc_data,
    delete_document,
    update_document_title,
)
from agentix.retrieval.usecases.document_upload_helpers import (
    UploadCommand,
    _build_object_key,
    _ensure_file_object_uploaded,
    _object_needs_reupload,
    _upload_content_type,
    normalize_folder_path,
)


async def upload_document(
    session: Session,
    command: UploadCommand,
) -> dict:
    """
    上传文档主流程（返回 UploadData）

    由 router 调用，router 负责参数校验与 HTTP 异常。
    """
    from agentix.retrieval.usecases.folder_service import resolve_folder_for_document

    file = command.file
    raw_filename = command.raw_filename
    kb_id = command.kb_id
    user_id = command.user_id
    folder_path = command.folder_path
    folder_id = command.folder_id
    idempotency_key = command.idempotency_key
    on_conflict = command.on_conflict
    replace_document_id = command.replace_document_id

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
            # 复用 FileObject（去重）
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

        if on_conflict == "replace":
            return _handle_replace(
                session,
                kb_id,
                replace_document_id or "",
                os.path.splitext(normalized)[0],
                file_object,
                normalized,
                size,
                sha256_hash,
                idempotency_key,
                auto_renamed,
            )

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

        if idempotency_key:
            existing_job = get_index_job_by_idempotency_key(session, idempotency_key)
            if existing_job:
                session.rollback()
                return UploadData(
                    document_id=document.id,
                    job_id=existing_job.id,
                    original_filename=raw_filename,
                    size_bytes=size,
                    sha256=sha256_hash,
                    is_duplicate=is_duplicate,
                    should_enqueue=False,
                ).model_dump()

        job_id, should_enqueue = ensure_kb_index_job(session, kb_id, idempotency_key=idempotency_key)
        session.commit()
        session.refresh(document)

        return UploadData(
            document_id=document.id,
            job_id=job_id,
            original_filename=raw_filename,
            size_bytes=size,
            sha256=sha256_hash,
            is_duplicate=is_duplicate,
            is_replace=False,
            auto_renamed=auto_renamed,
            should_enqueue=should_enqueue,
        ).model_dump()
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


class DocumentService:
    def __init__(self, session: Session):
        self.session = session

    def list_documents(
        self,
        *,
        kb_id: str,
        status_filter: str | None = None,
        page: int = 1,
        page_size: int = 20,
        folder_path_filter: str | None = None,
        folder_id_filter: str | None = None,
    ) -> tuple[list[dict], int]:
        from agentix.retrieval.infra.repos.kb_repo import get_kb_by_id
        from agentix.retrieval.infra.repos.document_repo import list_documents as _list_docs

        if not get_kb_by_id(self.session, kb_id):
            raise AppError(error_code=ErrorCode.KB_NOT_FOUND, message="知识库不存在", status_code=404)

        folder_path_filter = normalize_folder_path(folder_path_filter)
        items, total = _list_docs(
            self.session,
            kb_id,
            status_filter,
            page,
            page_size,
            folder_path_filter=folder_path_filter,
            folder_id_filter=folder_id_filter,
        )
        return ([_to_doc_data(d) for d in items], total)

    def update_document(self, *, document_id: str, title: str) -> dict:
        return update_document_title(self.session, document_id=document_id, title=title)

    def get_document(self, *, document_id: str) -> dict:
        from agentix.retrieval.infra.repos.document_repo import get_document_by_id

        doc = get_document_by_id(self.session, document_id)
        if not doc:
            raise AppError(error_code=ErrorCode.DOCUMENT_NOT_FOUND, message="文档不存在或已被删除", status_code=404)
        return _to_doc_data(doc)

    def get_document_file(self, *, document_id: str) -> tuple[str, str]:
        """
        获取文档原始文件路径（用于 PDF 预览/下载）。

        Returns:
            (object_key, filename)
        """
        from agentix.retrieval.infra.repos.document_repo import get_document_by_id

        doc = get_document_by_id(self.session, document_id)
        if not doc:
            raise AppError(
                error_code=ErrorCode.DOCUMENT_NOT_FOUND,
                message="文档不存在或已被删除",
                status_code=404,
            )

        if not doc.kb_file_id:
            raise AppError(
                error_code=ErrorCode.FILE_NOT_FOUND,
                message="文件记录不存在",
                status_code=404,
            )

        kb_file = get_kb_file_by_id(self.session, doc.kb_file_id)
        if not kb_file or not kb_file.file_object_id:
            raise AppError(
                error_code=ErrorCode.FILE_NOT_FOUND,
                message="文件记录不存在",
                status_code=404,
            )

        file_obj = get_file_object_by_id(self.session, kb_file.file_object_id)
        if not file_obj or not file_obj.object_key:
            raise AppError(
                error_code=ErrorCode.FILE_NOT_FOUND,
                message="文件不存在",
                status_code=404,
            )

        filename = doc.title or kb_file.original_filename or "document.pdf"
        if not filename.lower().endswith(".pdf"):
            filename = f"{filename}.pdf"
        return file_obj.object_key, filename

    def get_document_thumbnail(self, *, document_id: str) -> str:
        """返回缩略图 S3 object key；不存在时抛 AppError。"""
        from agentix.retrieval.infra.repos.document_repo import get_document_by_id

        doc = get_document_by_id(self.session, document_id)
        if not doc:
            raise AppError(
                error_code=ErrorCode.DOCUMENT_NOT_FOUND,
                message="文档不存在或已被删除",
                status_code=404,
            )
        key = (doc.thumbnail_key or "").strip()
        if not key:
            raise AppError(
                error_code=ErrorCode.FILE_NOT_FOUND,
                message="缩略图尚未生成",
                status_code=404,
                details={"document_id": document_id},
            )
        return key

    def delete_document(self, *, document_id: str, requested_by: str | None = None) -> dict:
        return delete_document(self.session, document_id, requested_by=requested_by)

    async def upload_documents_batch(
        self,
        *,
        files: list[object],
        kb_id: str,
        user_id: str,
        folder_path: Optional[str] = None,
        folder_id: Optional[str] = None,
        relative_paths: Optional[list[str]] = None,
        on_conflict: str = "create",
        idempotency_key: Optional[str] = None,
    ) -> dict:
        entries = build_batch_upload_entries(files, folder_path, relative_paths)
        batch_folder_id = folder_id if not relative_paths else None
        return await commit_documents_batch(
            self.session,
            kb_id=kb_id,
            user_id=user_id,
            entries=entries,
            on_conflict=on_conflict,
            idempotency_key=idempotency_key,
            folder_id=batch_folder_id,
        )

    async def upload_document(
        self,
        *,
        command: UploadCommand,
    ) -> dict:
        return await upload_document(
            session=self.session,
            command=command,
        )
