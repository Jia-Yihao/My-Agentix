"""
文档 Repo（infra 实现）：所有文档/文件对象/KbFile/Chunk 查询与持久化细节
"""

from sqlalchemy import func
from sqlmodel import Session, select

from agentix.retrieval.domain.models.document import Chunk, Document, FileObject, KbFile
from agentix.retrieval.domain.models.index import IndexArtifact, IndexArtifactManifest


def get_document_by_id(session: Session, doc_id: str) -> Document | None:
    return session.get(Document, doc_id)


def list_documents(
    session: Session,
    kb_id: str,
    status_filter: str | None = None,
    page: int = 1,
    page_size: int = 20,
    folder_path_filter: str | None = None,
    folder_id_filter: str | None = None,
) -> tuple[list[Document], int]:
    base = select(Document).where(Document.kb_id == kb_id)
    if status_filter:
        base = base.where(Document.status == status_filter)
    if folder_id_filter is not None:
        base = base.where(Document.folder_id == folder_id_filter)
    elif folder_path_filter is not None:
        base = base.where(Document.folder_path == folder_path_filter)
    total = session.exec(
        select(func.count()).select_from(base.subquery())
    ).one()
    items = session.exec(
        base.order_by(Document.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
    ).all()
    return list(items), int(total)


def find_document(
    session: Session,
    kb_id: str,
    folder_path: str | None,
    doc_title: str,
    *,
    folder_id: str | None = None,
) -> Document | None:
    if folder_id is not None:
        return session.exec(
            select(Document).where(
                Document.kb_id == kb_id,
                Document.folder_id == folder_id,
                Document.title == doc_title,
            )
        ).first()
    return session.exec(
        select(Document).where(
            Document.kb_id == kb_id,
            Document.folder_path == folder_path,
            Document.title == doc_title,
        )
    ).first()


def get_file_object_by_sha256(session: Session, sha256: str) -> FileObject | None:
    return session.exec(select(FileObject).where(FileObject.sha256 == sha256)).first()


def unlink_file_object_if_unused(session: Session, file_object_id: str) -> str | None:
    """若无 KbFile 引用则删除 FileObject，返回待删的磁盘路径（事务提交后删文件）"""
    if session.exec(select(KbFile).where(KbFile.file_object_id == file_object_id)).first():
        return None
    fo = session.get(FileObject, file_object_id)
    if not fo:
        return None
    path = fo.object_key
    session.delete(fo)
    return path


def delete_document_cascade(session: Session, document_id: str) -> tuple[str | None, str | None]:
    """
    级联删除文档及其关联数据（jobs/chunks/kb_file/file_object）

    Returns:
        (pdf_object_key, thumbnail_key) 待删除的对象 key（如有），需在 commit 后手动清理
    """
    doc = session.get(Document, document_id)
    if not doc:
        return None, None

    kb_file_id = doc.kb_file_id
    object_key_to_remove: str | None = None
    thumbnail_key_to_remove: str | None = (doc.thumbnail_key or "").strip() or None

    for chunk in session.exec(select(Chunk).where(Chunk.document_id == document_id)).all():
        session.delete(chunk)

    session.delete(doc)

    if kb_file_id:
        kb_file = session.get(KbFile, kb_file_id)
        if kb_file:
            file_object_id = kb_file.file_object_id
            session.delete(kb_file)

            if file_object_id:
                still_ref = (
                    session.exec(
                        select(KbFile).where(
                            KbFile.file_object_id == file_object_id,
                            KbFile.id != kb_file_id,
                        )
                    )
                    .first()
                )
                if not still_ref:
                    file_obj = session.get(FileObject, file_object_id)
                    if file_obj:
                        object_key_to_remove = file_obj.object_key
                        session.delete(file_obj)

    return object_key_to_remove, thumbnail_key_to_remove


def purge_deleted_document(session: Session, document_id: str) -> tuple[str | None, str | None]:
    """索引成功后物理清理已标记删除的文档及其对象。"""
    doc = session.get(Document, document_id)
    if not doc or doc.status != "deleted_pending_purge":
        return None, None
    return delete_document_cascade(session, document_id)


def delete_chunks_by_document_id(session: Session, document_id: str) -> None:
    for chunk in session.exec(select(Chunk).where(Chunk.document_id == document_id)).all():
        session.delete(chunk)


def document_ids_pending_purge(session: Session, kb_id: str) -> list[str]:
    """该 KB 下已标记删除、但尚未物理清理的文档 id。

    删除链路是「置 deleted_pending_purge → 排队重建 → 重建任务第 2 步清 chunk → 物理清理」，
    在重建任务跑到第 2 步之前，旧 active 向量索引里仍有这些文档的向量；若重建任务因
    worker 未消费等原因没跑，窗口会无限延长。因此检索侧必须立即把它们排除，
    才能兑现「删除请求后索引侧立即过滤」的约定。
    """
    rows = session.exec(
        select(Document.id).where(
            Document.kb_id == kb_id,
            Document.status == "deleted_pending_purge",
        )
    ).all()
    return [str(r) for r in rows]


def delete_chunks_for_document_ids(session: Session, document_ids: list[str]) -> None:
    if not document_ids:
        return
    for chunk in session.exec(select(Chunk).where(Chunk.document_id.in_(document_ids))).all():
        session.delete(chunk)

def has_any_document(session: Session, kb_id: str) -> bool:
    return (
        session.exec(
            select(func.count()).select_from(
                select(Document.id).where(Document.kb_id == kb_id).subquery()
            )
        ).one()
    ) > 0


def set_document_status(session: Session, document_id: str, status: str) -> None:
    doc = session.get(Document, document_id)
    if doc:
        doc.status = status
        session.add(doc)


def fail_documents_in_indexing(session: Session, kb_id: str) -> None:
    for doc in session.exec(
        select(Document).where(Document.kb_id == kb_id, Document.status == "indexing")
    ).all():
        doc.status = "failed"
        session.add(doc)


def resolve_document_file(session: Session, document_id: str) -> tuple[str, str]:
    """返回 (object_key, original_filename)。"""
    document = session.get(Document, document_id)
    if not document or not document.kb_file_id:
        raise RuntimeError(f"document or kb_file missing: {document_id}")
    kb_file = session.get(KbFile, document.kb_file_id)
    if not kb_file:
        raise RuntimeError(f"kb_file missing: {document_id}")
    file_object = session.get(FileObject, kb_file.file_object_id)
    if not file_object or not file_object.object_key:
        raise RuntimeError(f"file object missing: {document_id}")
    return file_object.object_key, kb_file.original_filename or "unknown"


def get_chunks_by_kb(session: Session, kb_id: str) -> list[Chunk]:
    """获取知识库所有 chunk。"""
    return list(session.exec(
        select(Chunk).join(Document, Document.id == Chunk.document_id).where(Document.kb_id == kb_id)
    ).all())


def get_chunks_for_index_artifact(
    session: Session, kb_id: str, artifact_id: str
) -> list[Chunk]:
    """仅返回当前活动索引产物实际包含、且仍处于 ready 状态的切片。"""
    return list(
        session.exec(
            select(Chunk)
            .join(Document, Document.id == Chunk.document_id)
            .join(
                IndexArtifactManifest,
                IndexArtifactManifest.document_id == Document.id,
            )
            .join(IndexArtifact, IndexArtifact.id == IndexArtifactManifest.artifact_id)
            .where(
                Document.kb_id == kb_id,
                Document.status == "ready",
                Document.indexed_artifact_id == artifact_id,
                IndexArtifactManifest.artifact_id == artifact_id,
                IndexArtifact.is_active == True,  # noqa: E712
            )
        ).all()
    )


def count_chunks_by_document(session: Session, document_id: str) -> int:
    """该文档残留的 chunk 数（删除后置校验用）。"""
    return int(
        session.exec(
            select(func.count()).select_from(
                select(Chunk.id).where(Chunk.document_id == document_id).subquery()
            )
        ).one()
    )


def manifest_has_document(session: Session, document_id: str) -> bool:
    """活动索引产物的 manifest 中是否仍登记该文档（是则其向量仍可被召回）。"""
    return (
        session.exec(
            select(func.count())
            .select_from(IndexArtifactManifest)
            .join(IndexArtifact, IndexArtifact.id == IndexArtifactManifest.artifact_id)
            .where(
                IndexArtifact.is_active == True,  # noqa: E712
                IndexArtifactManifest.document_id == document_id,
            )
        ).one()
    ) > 0


def has_any_chunk(session: Session, kb_id: str) -> bool:
    """
    判断知识库是否存在可检索内容（至少 1 个 chunk）。

    用于区分：
    - 有文档但解析不到文本（chunks=0）
    - 真正空知识库（documents=0）
    """
    return (
        session.exec(
            select(func.count())
            .select_from(Chunk)
            .join(Document, Document.id == Chunk.document_id)
            .where(Document.kb_id == kb_id)
        ).one()
    ) > 0


# -- M2.1: 等价数据访问方法（供 usecases 层替代 inline ORM） ------------------


def get_chunk_by_id(session: Session, chunk_id: str) -> Chunk | None:
    return session.get(Chunk, chunk_id)


def get_kb_file_by_id(session: Session, kb_file_id: str) -> KbFile | None:
    return session.get(KbFile, kb_file_id)


def get_file_object_by_id(session: Session, file_object_id: str) -> FileObject | None:
    return session.get(FileObject, file_object_id)


def list_chunks_by_document_id(session: Session, document_id: str) -> list[Chunk]:
    return list(
        session.exec(select(Chunk).where(Chunk.document_id == document_id)).all()
    )


def list_ready_documents_by_kb(session: Session, kb_id: str) -> list[Document]:
    return list(
        session.exec(
            select(Document).where(Document.kb_id == kb_id, Document.status == "ready")
        ).all()
    )


def count_all_documents(session: Session) -> int:
    return int(session.exec(select(func.count()).select_from(Document)).one())


def count_all_chunks(session: Session) -> int:
    return int(session.exec(select(func.count()).select_from(Chunk)).one())


def list_all_documents(session: Session, *, kb_id: str = "") -> list[Document]:
    stmt = select(Document)
    if kb_id:
        stmt = stmt.where(Document.kb_id == kb_id)
    stmt = stmt.order_by(Document.created_at.desc())
    return list(session.exec(stmt).all())


def list_documents_by_kb_with_folder(session: Session, kb_id: str) -> list[Document]:
    return list(
        session.exec(
            select(Document).where(
                Document.kb_id == kb_id,
                Document.folder_id.is_not(None),  # type: ignore[union-attr]
            )
        ).all()
    )


def get_document_file_meta(session: Session, document_id: str | None) -> dict:
    """
    文档的文件级元信息（引用展示用）。

    关联链路：Document.kb_file_id → KbFile.file_object_id → FileObject.size_bytes。
    文档不存在、或任一级关联缺失时对应字段为 None（不抛异常，兼容历史数据）。

    Returns:
        {"document_title", "folder_path", "file_name", "file_size", "file_ext"}
    """
    meta = {
        "document_title": None,
        "folder_path": None,
        "file_name": None,
        "file_size": None,
        "file_ext": None,
    }
    document = session.get(Document, document_id) if document_id else None
    if not document:
        return meta

    meta["document_title"] = document.title
    meta["folder_path"] = (document.folder_path or "").strip() or None

    kb_file = session.get(KbFile, document.kb_file_id) if document.kb_file_id else None
    file_name = ((kb_file.original_filename if kb_file else "") or "").strip()
    if not file_name:
        # 手动创建的文档没有 KbFile → 回退文档标题
        file_name = (document.title or "").strip()
    meta["file_name"] = file_name or None
    meta["file_ext"] = _extract_file_ext(file_name)

    if kb_file and kb_file.file_object_id:
        file_object = session.get(FileObject, kb_file.file_object_id)
        if file_object:
            meta["file_size"] = file_object.size_bytes

    return meta


def _extract_file_ext(file_name: str) -> str | None:
    """取小写扩展名（不含点）；无扩展名返回 None。"""
    if not file_name or "." not in file_name:
        return None
    ext = file_name.rpartition(".")[2].strip().lower()
    return ext or None