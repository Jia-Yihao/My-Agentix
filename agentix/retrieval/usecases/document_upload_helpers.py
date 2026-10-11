"""
文档上传辅助函数（纯逻辑 + 存储补传）

从 document_service.py 拆出（按域拆分批次）：路径归一化、object key 构建、
FileObject 补传判断与执行。上传主流程见 document_service.upload_document。
"""
import os
from dataclasses import dataclass
from typing import Optional

from sqlmodel import Session

from agentix.retrieval.api.response import ErrorCode
from agentix.retrieval.domain.models.document import FileObject
from agentix.retrieval.errors import AppError
from agentix.retrieval.infra.storage.s3_minio import head_meta, head_object, upload_file


@dataclass(frozen=True)
class UploadCommand:
    """HTTP 无关的上传命令；具体文件对象由 API 适配层注入。"""

    file: object
    raw_filename: str
    kb_id: str
    user_id: str
    folder_path: Optional[str] = None
    folder_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    on_conflict: str = "create"
    replace_document_id: Optional[str] = None


def _upload_content_type(file: object) -> str:
    return str(getattr(file, "content_type", None) or "application/pdf")


def normalize_folder_path(folder_path: Optional[str]) -> Optional[str]:
    """
    将用户输入的 folder_path 归一化为稳定表示：
    - None / "" / "/" -> None（根目录）
    - "\\" -> "/"
    - 去首尾 "/"
    - 去除空段（"a//b" -> "a/b"）
    - 禁止 "." / ".." 段，避免路径穿越与脏数据
    """
    if folder_path is None:
        return None
    p = folder_path.strip()
    if p in {"", "/"}:
        return None

    p = p.replace("\\", "/").strip("/")
    parts = [seg for seg in p.split("/") if seg]
    if not parts:
        return None
    if any(seg in {".", ".."} for seg in parts):
        raise AppError(
            error_code=ErrorCode.VALIDATION_ERROR,
            message="非法 folder_path",
            status_code=400,
        )
    return "/".join(parts)


def _safe_filename(name: str) -> str:
    # 最小防御：只取 basename，且避免空名
    base = os.path.basename((name or "").replace("\\", "/")).strip() or "document.pdf"
    return base


def _build_object_key(*, user_id: str, kb_id: str, file_object_id: str, raw_filename: str) -> str:
    safe = _safe_filename(raw_filename)
    return f"u/{user_id}/kb/{kb_id}/doc/{file_object_id}/{safe}"


def _object_needs_reupload(*, object_key: str | None, size_bytes: int | None) -> bool:
    """
    去重复用 FileObject 时，判断是否需要用本次上传文件补传对象存储。

    触发条件：
    - object_key 为空
    - 对象不存在
    - DB size_bytes 为 0/空
    - 对象存在但 content_length 为 0（防止空 PDF 进入索引）
    """
    key = (object_key or "").strip()
    if not key:
        return True
    if not head_object(key):
        return True
    if (size_bytes or 0) <= 0:
        return True
    try:
        meta = head_meta(key)
        if int(meta.get("content_length") or 0) <= 0:
            return True
    except Exception:
        # 元信息读取失败：保守起见补传，避免坏对象继续传播
        return True
    return False


def _ensure_file_object_uploaded(
    *,
    session: Session,
    file_object: FileObject,
    temp_path: str,
    content_type: str,
    user_id: str,
    kb_id: str,
    raw_filename: str,
    size_bytes: int,
) -> None:
    """确保 FileObject 对应对象在存储中非空；必要时为其分配 object_key 并补传。"""
    if not _object_needs_reupload(object_key=file_object.object_key, size_bytes=file_object.size_bytes):
        return

    key = (file_object.object_key or "").strip()
    if not key:
        key = _build_object_key(
            user_id=user_id,
            kb_id=kb_id,
            file_object_id=file_object.id,
            raw_filename=raw_filename,
        )
        file_object.object_key = key

    upload_file(temp_path, key, content_type=content_type)
    file_object.size_bytes = size_bytes
    session.add(file_object)


def _folder_and_filename_from_relative(rel: str) -> tuple[Optional[str], str]:
    """relative_paths 单项：'a/b.pdf' -> (folder 'a', filename 'b.pdf')"""
    rel = rel.replace("\\", "/").strip()
    if not rel:
        raise AppError(error_code=ErrorCode.VALIDATION_ERROR, message="relative_paths 含空项", status_code=400)
    name = os.path.basename(rel) or "document.pdf"
    parent = os.path.dirname(rel).replace("\\", "/").strip()
    if parent in {"", ".", "/"}:
        fp: Optional[str] = None
    else:
        fp = normalize_folder_path(parent)
    return fp, name
