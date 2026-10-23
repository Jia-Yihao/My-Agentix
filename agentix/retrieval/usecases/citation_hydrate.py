"""
引用（citation）还原：从 Answer.citations 还原完整引用信息。

chat_service（聊天流式落库侧）与 conversation_service（历史会话回放侧）共用。

置于 usecases 而非 domain/services：本模块要经 infra/repos 访问 DB（I/O），
而 domain/ 是纯领域层、不得依赖 infra。放 usecases 既满足分层，也避免
usecases 之间互相 import（两处均从本模块导入，方向单向）。
"""
import json

from sqlmodel import Session

from agentix.retrieval.infra.repos.document_repo import get_chunk_by_id, get_document_file_meta

# 文件级元信息字段：历史 Answer.citations 里没有这些键 → 还原为 None（向后兼容）
FILE_META_KEYS = ("document_title", "file_name", "folder_path", "file_size", "file_ext")


def hydrate_citations_from_stored(session: Session, stored) -> list[dict]:
    """从 Answer.citations 还原完整引用（兼容仅存 chunk_id 的旧数据）。"""
    if not stored:
        return []
    if isinstance(stored, str):
        try:
            stored = json.loads(stored)
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(stored, list):
        return []

    out: list[dict] = []
    for item in stored:
        if not isinstance(item, dict):
            continue
        chunk_id = item.get("chunk_id")
        if not chunk_id:
            continue
        if item.get("snippet"):
            out.append(
                {
                    "chunk_id": chunk_id,
                    "document_id": item.get("document_id"),
                    "page_no": item.get("page_no"),
                    "snippet": item["snippet"],
                    "source": item.get("source"),
                    # 有效性徽标/替代提示随历史透传（老数据无此键 → None，前端容错）
                    "effective_status": item.get("effective_status"),
                    "replaced_by": item.get("replaced_by"),
                    **{key: item.get(key) for key in FILE_META_KEYS},
                }
            )
            continue
        chunk = get_chunk_by_id(session, chunk_id)
        if not chunk:
            continue
        document_id = chunk.document_id
        file_meta = get_document_file_meta(session, document_id)
        source_name = (item.get("source") or "").strip()
        if document_id and not source_name:
            source_name = (file_meta["document_title"] or "").strip() or source_name
        content = chunk.content or ""
        snippet = content[:200] + ("..." if len(content) > 200 else "")
        out.append(
            {
                "chunk_id": chunk_id,
                "document_id": document_id,
                "page_no": chunk.page_no,
                "snippet": snippet,
                "source": source_name or None,
                "effective_status": item.get("effective_status"),
                "replaced_by": item.get("replaced_by"),
                **file_meta,
            }
        )
    return out
