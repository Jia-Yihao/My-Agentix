"""向量库管理用例：封装统计、列表、删除和分类的事务编排。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from sqlmodel import Session

from agentix.retrieval.domain.models.document import Chunk
from agentix.retrieval.infra.repos.document_repo import (
    count_all_chunks,
    count_all_documents,
    delete_chunks_by_document_id,
    get_document_by_id,
    list_all_documents,
    list_chunks_by_document_id,
)


def get_stats(session: Session) -> dict[str, int]:
    return {
        "total_documents": count_all_documents(session),
        "total_chunks": count_all_chunks(session),
    }


def list_documents(
    session: Session,
    *,
    search: str = "",
    category: str = "",
    kb_id: str = "",
    page: int = 1,
    page_size: int = 20,
    classify: Callable[[list[Chunk]], dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    result = []
    for doc in list_all_documents(session, kb_id=kb_id):
        chunks = list_chunks_by_document_id(session, doc.id)
        classification = classify(list(chunks))
        lowered = search.lower()
        if lowered and not (
            lowered in (doc.title or "").lower()
            or lowered in (doc.id or "").lower()
            or lowered in " ".join(c.content for c in chunks if c.content).lower()
            or any(lowered in tag.lower() for tag in classification.get("tags", []))
        ):
            continue
        if category and classification.get("category") != category:
            continue
        result.append({
            "id": doc.id,
            "title": doc.title or doc.id,
            "kb_id": doc.kb_id,
            "status": doc.status,
            "size": getattr(doc, "file_size", 0),
            "chunk_count": len(chunks),
            "category": classification["category"],
            "tags": classification["tags"],
            "strategy_detected": classification.get("strategy_detected", ""),
            "created_at": str(doc.created_at) if doc.created_at else "",
        })
    total = len(result)
    start = max(0, page - 1) * page_size
    return result[start:start + page_size], total


def delete_document(session: Session, document_id: str) -> None:
    delete_chunks_by_document_id(session, document_id)
    document = get_document_by_id(session, document_id)
    if document:
        session.delete(document)
    session.commit()


def classify_documents(
    session: Session,
    document_ids: list[str],
    classify: Callable[[list[Chunk]], dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for document_id in document_ids:
        document = get_document_by_id(session, document_id)
        if not document:
            continue
        chunks = list_chunks_by_document_id(session, document_id)
        classification = classify(list(chunks))
        if hasattr(document, "metadata"):
            try:
                metadata = json.loads(document.metadata) if isinstance(document.metadata, str) else dict(document.metadata or {})
            except (json.JSONDecodeError, TypeError):
                metadata = {}
            metadata["classification"] = classification
            document.metadata = json.dumps(metadata, ensure_ascii=False)
            session.add(document)
        results[document_id] = classification
    session.commit()
    return results
