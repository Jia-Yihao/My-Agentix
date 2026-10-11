"""
对话检索范围：纯领域逻辑
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agentix.retrieval.domain.models.chat import Conversation
from agentix.retrieval.domain.models.document import Document
from agentix.retrieval.domain.models.folder import KbFolder

SCOPE_KB = "kb"
SCOPE_FOLDER = "folder"
SCOPE_FILE = "file"


def scope_from_request(*, document_id: str | None, folder_id: str | None) -> tuple[str, str | None]:
    if document_id:
        return SCOPE_FILE, document_id
    if folder_id:
        return SCOPE_FOLDER, folder_id
    return SCOPE_KB, None


def resolve_effective_scope(
    *,
    conversation: Conversation | None,
    document_id: str | None,
    folder_id: str | None,
) -> tuple[str, str | None]:
    """续聊用会话已存范围；新会话用本次请求。"""
    if conversation:
        st = getattr(conversation, "scope_type", None) or SCOPE_KB
        sid = getattr(conversation, "scope_id", None)
        return st, sid
    return scope_from_request(document_id=document_id, folder_id=folder_id)


def document_ids_from_scope(
    scope_type: str,
    scope_id: str | None,
    *,
    folder_doc_ids: list[str],
) -> list[str] | None:
    """返回允许检索的 document_id 列表；整库返回 None。"""
    if scope_type == SCOPE_KB:
        return None
    if scope_type == SCOPE_FILE:
        return [scope_id] if scope_id else []
    if scope_type == SCOPE_FOLDER:
        return folder_doc_ids
    return []


def build_faiss_filter(document_ids: list[str] | None) -> dict[str, Any] | Callable[[dict], bool] | None:
    if not document_ids:
        return None
    if len(document_ids) == 1:
        return {"document_id": document_ids[0]}
    allowed = set(document_ids)
    return lambda meta: meta.get("document_id") in allowed


def meta_matches_filter(
    meta: dict | None,
    metadata_filter: dict[str, Any] | Callable[[dict], bool] | None,
) -> bool:
    """与 :func:`build_faiss_filter` 同源的过滤判定（本地复算版）。

    FAISS 把 filter 交给向量库执行；BM25 sparse 路（R-P1-1）没有执行器，
    必须在应用侧用**同一语义**复算，否则同一次查询的 scope 收窄对两通道
    不一致（file/folder scope 会被 sparse 路穿透）：

    - ``None`` → 全部通过；
    - ``dict`` → 逐键等值（值为 list/set/tuple 视为 IN，与 langchain FAISS
      的 ``_normalize_dict`` 语义一致）；
    - ``callable`` → 直接调用；异常 fail-closed 记为不通过（与
      :func:`merge_exclusion_filter` 的组合语义一致）。
    """
    if metadata_filter is None:
        return True
    meta = meta or {}
    if callable(metadata_filter):
        try:
            return bool(metadata_filter(meta))
        except Exception:
            return False
    if isinstance(metadata_filter, dict):
        for key, want in metadata_filter.items():
            have = meta.get(key)
            if isinstance(want, (list, tuple, set, frozenset)):
                if have not in want:
                    return False
            elif have != want:
                return False
        return True
    return False


def merge_exclusion_filter(
    base_filter: dict[str, Any] | Callable[[dict], bool] | None,
    excluded_ids: list[str],
) -> dict[str, Any] | Callable[[dict], bool] | None:
    """在 base_filter 基础上排除 excluded_ids 中的文档（待审批文档对检索不可见）。

    - 无 excluded：原样返回 base_filter；
    - base_filter 为 None（整库检索）：返回排除 filter；
    - base_filter 为 dict（单文档）：若该文档被排除 → 永不匹配，否则原样返回；
    - base_filter 为 Callable：组合为「原条件 且 document_id 不在排除集」。
    """
    if not excluded_ids:
        return base_filter
    excluded = set(excluded_ids)

    if base_filter is None:
        return lambda meta: meta.get("document_id") not in excluded

    if isinstance(base_filter, dict):
        doc_id = base_filter.get("document_id")
        if doc_id is not None and doc_id in excluded:
            return lambda meta: False
        return base_filter

    def _combined(meta: dict) -> bool:
        try:
            return bool(base_filter(meta)) and meta.get("document_id") not in excluded
        except Exception:
            return False

    return _combined


def scope_cache_token(scope_type: str, scope_id: str | None) -> str:
    return f"{scope_type}:{scope_id or ''}"


def retrieval_cache_scope_token(scope_token: str, document_id: str | None) -> str:
    if document_id:
        return f"{scope_token}@doc:{document_id}"
    return scope_token


def check_scope_file(
    scope_id: str | None,
    doc: Document | None,
    kb_id: str,
) -> str | None:
    """返回 'missing_id' | 'not_found' | None（合法）。"""
    if not scope_id:
        return "missing_id"
    if not doc or doc.kb_id != kb_id:
        return "not_found"
    return None


def check_scope_folder(
    scope_id: str | None,
    folder: KbFolder | None,
    kb_id: str,
) -> str | None:
    """返回 'missing_id' | 'not_found' | None（合法）。"""
    if not scope_id:
        return "missing_id"
    if not folder or folder.kb_id != kb_id:
        return "not_found"
    return None


def match_document_id_by_title(text: str, docs: list[Document]) -> str | None:
    """
    从用户问题中识别明确提到的文档标题，返回对应 document_id。
    优先匹配更长标题，避免「2023_PDF3」误命中「2023_PDF2」。
    """
    query = (text or "").strip().lower()
    if not query:
        return None

    best: tuple[int, str] | None = None
    for doc in docs:
        title = (doc.title or "").strip()
        if len(title) < 2:
            continue
        if title.lower() in query:
            key = (len(title), doc.id)
            if best is None or key > best:
                best = key
    return best[1] if best else None


def format_scope_prompt_label(
    scope_type: str,
    scope_id: str | None,
    *,
    doc_title: str | None = None,
    folder_name: str | None = None,
) -> str:
    if scope_type == SCOPE_KB:
        return "整个知识库"
    if scope_type == SCOPE_FILE and scope_id:
        title = doc_title if doc_title else scope_id
        return f"单个文件「{title}」"
    if scope_type == SCOPE_FOLDER and scope_id:
        name = folder_name if folder_name else scope_id
        return f"文件夹「{name}」（含子文件夹）"
    return "当前范围"
