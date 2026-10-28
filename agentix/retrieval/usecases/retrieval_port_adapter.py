"""RetrievalPort 本地 Adapter——检索能力域对编排层的唯一出口。

依据 design.md §2.4.4：
- 位置：``agentix/retrieval/usecases/retrieval_port_adapter.py``
- 职责：编排 ``get_active_index_artifact → get_or_build_retriever（默认直调
  build_retriever，flag 开时复用进程内实例）→
  retrieve_scoped_docs_cached → rerank_documents → get_citations_from_docs``
  链路，将检索域用例适配为 ``RetrievalPort`` 协议返回结构。
- 降级语义：Milvus 超时 / worker 崩 / 索引未就绪 → ``status="degraded"``；
  空命中 → ``status="empty"``；正常 → ``status="ok"``。

租户 fail-closed（R-P0-2）：``tenant_id`` 必传且非空白——三个端口方法在触达
任何存储访问之前先校验，空值直接抛 ``ValueError``（契约违规=响亮失败，
不降级兜底，杜绝"无租户上下文"的静默跨租户读写）；基础设施故障仍按上文
降级语义走 ``status="degraded"``。行级权限（row-level filter）另批下沉
检索层，不在 P0 最小集内。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Optional

from agentix.domain.ports.retrieval import (
    IndexStatus,
    RetrievalChunk,
    RetrievalCitation,
    RetrievalResult,
)

logger = logging.getLogger(__name__)


def _require_tenant(tenant_id: Optional[str]) -> str:
    """R-P0-2 租户 fail-closed 校验：空/空白/None ⇒ ValueError。

    必须在任何 ``session_scope`` / 仓储访问**之前**调用（不放进 try，
    避免被宽泛异常处理吞成 degraded 的"成功假象"）。
    """
    tid = (tenant_id or "").strip()
    if not tid:
        raise ValueError(
            "tenant_id is required (R-P0-2 fail-closed): "
            "empty tenant must never reach retrieval operations"
        )
    return tid


@contextmanager
def _default_session_scope():
    from agentix.retrieval.infra.db.session import session_scope

    with session_scope() as session:
        yield session


class RetrievalPortAdapter:
    """``RetrievalPort`` 的本地实现，委托检索域用例。

    可注入 ``session_scope_factory`` 便于测试替换；生产默认使用
    ``agentix.retrieval.infra.db.session.session_scope``。
    """

    def __init__(
        self,
        *,
        session_scope_factory: Optional[Any] = None,
        cache_redis: Optional[Any] = None,
    ) -> None:
        self._session_scope = session_scope_factory or _default_session_scope
        self._cache_redis = cache_redis

    async def search(
        self,
        *,
        query: str,
        kb_id: str,
        tenant_id: str,
        top_k: int = 5,
    ) -> RetrievalResult:
        _require_tenant(tenant_id)  # R-P0-2：在任何存储访问之前 fail-closed

        from agentix.retrieval.infra.repos.index_repo import get_active_index_artifact
        from agentix.retrieval.infra.vectorstore.retriever_cache import (
            get_or_build_retriever,
        )
        from agentix.retrieval.usecases.chat_service import (
            get_citations_from_docs,
            retrieve_scoped_docs_cached,
        )
        from agentix.retrieval.settings import settings

        q = (query or "").strip()
        if not q:
            return RetrievalResult(status="empty", chunks=[], error=None)

        try:
            with self._session_scope() as session:
                artifact = get_active_index_artifact(session, kb_id)
                if not artifact:
                    return RetrievalResult(
                        status="degraded", chunks=[], error="indexing"
                    )

                artifact_id = getattr(artifact, "id", "") or str(
                    getattr(artifact, "version", "")
                )

                try:
                    # R-P3：默认关 ⇒ 等价于 build_retriever；开启则复用进程内
                    # 已构造的 (retriever, vectorstore)，省掉每次检索的客户端
                    # 重建 + FAISS 反序列化（README §15.2 口径③）。
                    _, vectorstore = get_or_build_retriever(artifact.index_path)
                except FileNotFoundError:
                    return RetrievalResult(
                        status="degraded", chunks=[], error="index_not_found"
                    )

                docs = await retrieve_scoped_docs_cached(
                    kb_id=kb_id,
                    question=q,
                    search_k=top_k,
                    metadata_filter=None,
                    vectorstore=vectorstore,
                    cache_redis=self._cache_redis,
                    artifact_id=artifact_id,
                    scope_token="",
                    session=session,
                    tenant_id=tenant_id,  # W2：端口路径显式租户，缓存键免查库分池
                )

                if getattr(settings, "RERANKER_ENABLED", False) and len(docs) > 1:
                    from agentix.retrieval.infra.rag.rerank import (
                        dynamic_rerank_skip_reason,
                        rerank_documents,
                    )

                    # W2 动态跳过（默认关）：候选过少/首位分已高 ⇒ 保序直过
                    if dynamic_rerank_skip_reason(docs) is None:
                        docs = await rerank_documents(
                            q, docs, getattr(settings, "RERANKER_MODEL", None)
                        )

                if not docs:
                    return RetrievalResult(
                        status="empty", chunks=[], error=None
                    )

                citations = get_citations_from_docs(docs, session)
                chunks = self._build_chunks(docs, citations, kb_id, tenant_id)
                return RetrievalResult(status="ok", chunks=chunks, error=None)

        except Exception as e:
            logger.exception("RetrievalPortAdapter.search failed: %s", e)
            return RetrievalResult(
                status="degraded", chunks=[], error="retrieval_unavailable"
            )

    async def delete_by_doc(self, *, doc_id: str, tenant_id: str) -> None:
        _require_tenant(tenant_id)  # R-P0-2：删除操作同样 fail-closed

        from agentix.retrieval.usecases.vector_db_service import delete_document

        with self._session_scope() as session:
            delete_document(session, doc_id)

    async def index_status(self, *, kb_id: str, tenant_id: str) -> IndexStatus:
        _require_tenant(tenant_id)  # R-P0-2：状态查询同样 fail-closed

        from agentix.retrieval.infra.repos.index_repo import get_active_index_artifact

        try:
            with self._session_scope() as session:
                artifact = get_active_index_artifact(session, kb_id)
                if not artifact:
                    return IndexStatus(
                        kb_id=kb_id,
                        status="unknown",
                        progress=None,
                        error_message=None,
                    )
                artifact_status = getattr(artifact, "status", "unknown")
                if artifact_status in ("active", "ready"):
                    port_status = "ready"
                elif artifact_status in ("building", "indexing", "pending"):
                    port_status = "indexing"
                elif artifact_status in ("failed", "error"):
                    port_status = "failed"
                else:
                    port_status = "unknown"
                return IndexStatus(
                    kb_id=kb_id,
                    status=port_status,
                    progress=getattr(artifact, "progress", None),
                    error_message=getattr(artifact, "error_message", None),
                )
        except Exception as e:
            logger.exception("RetrievalPortAdapter.index_status failed: %s", e)
            return IndexStatus(
                kb_id=kb_id,
                status="unknown",
                progress=None,
                error_message=str(e),
            )

    @staticmethod
    def _build_chunks(
        docs: list,
        citations: list[dict],
        kb_id: str,
        tenant_id: str,
    ) -> list[RetrievalChunk]:
        chunks: list[RetrievalChunk] = []
        for i, doc in enumerate(docs):
            meta = doc.metadata or {}
            citation_data = citations[i] if i < len(citations) else {}
            citation = RetrievalCitation(
                kb_id=kb_id,
                doc_id=citation_data.get("document_id", meta.get("document_id", "")),
                doc_title=citation_data.get("source", citation_data.get("document_title", "")),
                page=citation_data.get("page_no", meta.get("page")),
                tenant_id=tenant_id,
            )
            chunks.append(
                RetrievalChunk(
                    chunk_id=meta.get("chunk_id", ""),
                    content=doc.page_content,
                    score=float(meta.get("_retrieval_score", 0.0)),
                    citation=citation,
                )
            )
        return chunks


def create_retrieval_port(
    *,
    session_scope_factory: Optional[Any] = None,
    cache_redis: Optional[Any] = None,
) -> RetrievalPortAdapter:
    """工厂函数：创建 RetrievalPort 本地 Adapter 实例。"""
    return RetrievalPortAdapter(
        session_scope_factory=session_scope_factory,
        cache_redis=cache_redis,
    )
