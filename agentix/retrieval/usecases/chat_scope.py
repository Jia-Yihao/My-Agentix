"""
对话检索范围：薄兼容层（re-export + AppError 编排）

纯逻辑见 domain/services/chat_scope.py；查库见 infra/repos/scope_repo.py。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlmodel import Session

from agentix.retrieval.api.response import ErrorCode
from agentix.retrieval.domain.services import chat_scope as scope_domain
from agentix.retrieval.errors import AppError
from agentix.retrieval.infra.repos import scope_repo

# 常量与纯函数 re-export（调用方可继续 from agentix.retrieval.usecases.chat_scope import ...）
SCOPE_KB = scope_domain.SCOPE_KB
SCOPE_FOLDER = scope_domain.SCOPE_FOLDER
SCOPE_FILE = scope_domain.SCOPE_FILE

scope_from_request = scope_domain.scope_from_request
resolve_effective_scope = scope_domain.resolve_effective_scope
build_faiss_filter = scope_domain.build_faiss_filter
merge_exclusion_filter = scope_domain.merge_exclusion_filter
scope_cache_token = scope_domain.scope_cache_token
retrieval_cache_scope_token = scope_domain.retrieval_cache_scope_token


def validate_scope(
    session: Session,
    *,
    kb_id: str,
    scope_type: str,
    scope_id: str | None,
) -> None:
    if scope_type == SCOPE_KB:
        return
    if scope_type == SCOPE_FILE:
        err = scope_domain.check_scope_file(
            scope_id, scope_repo.fetch_document(session, scope_id or ""), kb_id
        )
        if err == "missing_id":
            raise AppError(
                error_code=ErrorCode.DOCUMENT_NOT_FOUND,
                message="文件范围缺少 document_id",
                status_code=400,
            )
        if err == "not_found":
            raise AppError(
                error_code=ErrorCode.DOCUMENT_NOT_FOUND,
                message="文档不存在",
                status_code=404,
            )
        return
    if scope_type == SCOPE_FOLDER:
        err = scope_domain.check_scope_folder(
            scope_id, scope_repo.fetch_folder(session, scope_id or ""), kb_id
        )
        if err == "missing_id":
            raise AppError(
                error_code=ErrorCode.KB_NOT_FOUND,
                message="文件夹范围缺少 folder_id",
                status_code=400,
            )
        if err == "not_found":
            raise AppError(
                error_code=ErrorCode.KB_NOT_FOUND,
                message="文件夹不存在",
                status_code=404,
            )
        return
    raise AppError(
        error_code=ErrorCode.INTERNAL_ERROR,
        message=f"不支持的 scope_type: {scope_type}",
        status_code=400,
    )


def document_ids_for_scope(
    session: Session,
    *,
    kb_id: str,
    scope_type: str,
    scope_id: str | None,
) -> list[str] | None:
    return scope_repo.document_ids_for_scope(
        session, kb_id=kb_id, scope_type=scope_type, scope_id=scope_id
    )


def count_documents_in_scope(
    session: Session,
    *,
    kb_id: str,
    scope_type: str,
    scope_id: str | None,
) -> int:
    return scope_repo.count_documents_in_scope(
        session, kb_id=kb_id, scope_type=scope_type, scope_id=scope_id
    )


def list_document_titles_in_scope(
    session: Session,
    *,
    kb_id: str,
    scope_type: str,
    scope_id: str | None,
    limit: int = 30,
) -> list[str]:
    return scope_repo.list_document_titles_in_scope(
        session, kb_id=kb_id, scope_type=scope_type, scope_id=scope_id, limit=limit
    )


def resolve_document_id_from_question(
    session: Session,
    *,
    kb_id: str,
    scope_type: str,
    scope_id: str | None,
    text: str,
) -> str | None:
    return scope_repo.resolve_document_id_from_question(
        session,
        kb_id=kb_id,
        scope_type=scope_type,
        scope_id=scope_id,
        text=text,
    )


def document_title_for_id(session: Session, document_id: str | None) -> str | None:
    return scope_repo.document_title_for_id(session, document_id)


def non_retrievable_document_ids(session: Session, kb_id: str) -> list[str]:
    """对检索不可见的文档 id（两类合并去重）。

    1. 存在未决删除审批的文档（待审批态，不应被检索到）；
    2. 已标记 ``deleted_pending_purge`` 的文档 —— 其 chunk 要等索引重建任务跑到第 2 步
       才被清掉，在那之前旧 active 索引里仍有其向量，仅靠重建无法保证「删除后立即不可见」。

    调用方应把它交给 ``merge_exclusion_filter`` 作为排除集合；点名检索（收窄到单文件）时
    也要用它判断，否则收窄会把排除语义丢掉。
    """
    from agentix.retrieval.infra.repos.approval_repo import pending_approval_document_ids
    from agentix.retrieval.infra.repos.document_repo import document_ids_pending_purge

    merged = [
        *pending_approval_document_ids(session, kb_id),
        *document_ids_pending_purge(session, kb_id),
    ]
    return list(dict.fromkeys(merged))  # 去重且保持稳定顺序


def retrieval_filter_for_question(
    session: Session,
    *,
    kb_id: str,
    scope_type: str,
    scope_id: str | None,
    base_filter: dict[str, Any] | Callable[[dict], bool] | None,
    text: str,
) -> tuple[dict[str, Any] | Callable[[dict], bool] | None, str | None]:
    """
    在会话范围 filter 基础上，若问题点名某文档则收窄到该文件。
    返回 (filter, matched_document_id)。

    对检索不可见的文档（待审批 / 已标记删除待清理）：若命中的文档属于这两类，则不收窄
    （保持 base_filter 的排除语义），避免该类文档被点名检索到。
    """
    if scope_type == SCOPE_FILE:
        return base_filter, scope_id

    doc_id = resolve_document_id_from_question(
        session,
        kb_id=kb_id,
        scope_type=scope_type,
        scope_id=scope_id,
        text=text,
    )
    if not doc_id:
        return base_filter, None
    # 不可检索文档不收窄（保持 base_filter 的排除语义）
    if doc_id in set(non_retrievable_document_ids(session, kb_id)):
        return base_filter, None
    return build_faiss_filter([doc_id]), doc_id


def scope_prompt_label(
    session: Session,
    *,
    kb_id: str,
    scope_type: str,
    scope_id: str | None,
) -> str:
    doc_title: str | None = None
    folder_name: str | None = None
    if scope_type == SCOPE_FILE and scope_id:
        doc = scope_repo.fetch_document(session, scope_id)
        if doc and doc.kb_id == kb_id:
            doc_title = doc.title
    elif scope_type == SCOPE_FOLDER and scope_id:
        folder = scope_repo.fetch_folder(session, scope_id)
        if folder and folder.kb_id == kb_id:
            folder_name = folder.name
    return scope_domain.format_scope_prompt_label(
        scope_type,
        scope_id,
        doc_title=doc_title,
        folder_name=folder_name,
    )
