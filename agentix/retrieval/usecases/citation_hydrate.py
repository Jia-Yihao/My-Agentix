"""
引用（citation）还原：从 Answer.citations 还原完整引用信息。

chat_service（聊天流式落库侧）与 conversation_service（历史会话回放侧）共用。

置于 usecases 而非 domain/services：本模块要经 infra/repos 访问 DB（I/O），
而 domain/ 是纯领域层、不得依赖 infra。放 usecases 既满足分层，也避免
usecases 之间互相 import（两处均从本模块导入，方向单向）。

按 document 聚合：一次 IN 查全部 chunk、三次 IN 查全部文件级元信息，
不再逐条 ``get_chunk_by_id`` + ``get_document_file_meta``（原本 2N 次查询）。
"""
import json

from sqlmodel import Session

from agentix.retrieval.infra.repos.document_repo import (
    _empty_file_meta,
    get_chunks_by_ids,
    get_document_file_metas,
)

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

    items = [x for x in stored if isinstance(x, dict) and x.get("chunk_id")]

    # 已有 snippet 的条目无需回表；其余按 chunk_id / document_id 批量回表一次。
    need_db = [x for x in items if not x.get("snippet")]
    chunk_ids = [str(x["chunk_id"]) for x in need_db]
    chunks = get_chunks_by_ids(session, chunk_ids)
    doc_ids = [str(chunk.document_id) for chunk in chunks.values() if chunk.document_id]
    file_metas = get_document_file_metas(session, doc_ids)

    out: list[dict] = []
    for item in items:
        chunk_id = item.get("chunk_id")
        if item.get("snippet"):
            out.append(
                {
                    "chunk_id": chunk_id,
                    "document_id": item.get("document_id"),
                    "page_no": item.get("page_no"),
                    "snippet": item["snippet"],
                    "source": item.get("source"),
                    # 召回溯源：标号与引用状态随历史透传（老数据无此键 → None）
                    "ref": item.get("ref"),
                    "cited": item.get("cited"),
                    # 有效性徽标/替代提示随历史透传（老数据无此键 → None，前端容错）
                    "effective_status": item.get("effective_status"),
                    "replaced_by": item.get("replaced_by"),
                    **{key: item.get(key) for key in FILE_META_KEYS},
                }
            )
            continue
        chunk = chunks.get(str(chunk_id))
        if not chunk:
            continue
        document_id = chunk.document_id
        file_meta = file_metas.get(str(document_id), _empty_file_meta())
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
                "ref": item.get("ref"),
                "cited": item.get("cited"),
                "effective_status": item.get("effective_status"),
                "replaced_by": item.get("replaced_by"),
                **file_meta,
            }
        )
    return out
