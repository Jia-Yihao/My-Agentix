"""知识检索网关——法律编排层访问检索能力域的唯一入口。

R5.2 下线 ``infrastructure.knowledge`` 的 DEPRECATED 兼容 shim 后，原
kg_sub_graph / file_handler / general_handler 等编排侧调用统一改接本网关。
网关仅做协议转换：``RetrievalPort.search`` 返回 ``RetrievalResult``，
编排层期望 ``List[Dict]``（content/score/chunk_id/doc_id/metadata）。

重排说明：rerank 已内置于 RetrievalPortAdapter（见 R1.8 归档记录），
本网关的 ``reranker`` 为语义兼容桩（``enabled=False``），编排层的
外部重排分支因此跳过，检索质量由能力域内部重排保证。

kb_id 通过构造参数或环境变量 LEGELBOT_DEFAULT_KB_ID 指定，默认
"default"；tenant_id 取 LEGELBOT_DEFAULT_TENANT_ID，默认 "default"。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def get_retrieval_port():
    """M5 单门收敛：应用侧获取 ``RetrievalPort`` 的唯一装配点。

    应用层（handlers / 图节点 / 服务）一律经本函数取 Port，不再直连
    ``retrieval.usecases.retrieval_port_adapter``——对外装配收敛到网关单文件。
    """
    from agentix.retrieval.usecases.retrieval_port_adapter import create_retrieval_port

    return create_retrieval_port()


class _RerankerCompatStub:
    """重排兼容桩——开/关/熔断三态的「开关」与内层同源（R-P0-3）。

    ``enabled`` 不再是写死的 False（死分支）：构造时与
    ``RetrievalPortAdapter`` 内层重排同取 ``RERANKER_ENABLED``
    （默认 false ⇒ 字节等价）。三态语义：

    - **关**：``enabled=False``，编排层外部重排分支跳过（既有默认行为）；
    - **开**：``enabled=True``，编排层按 rerank 分数双阈值过滤
      （``KB_POSTGRES_RERANK_THRESHOLD``）；重排本体由检索域内层 / 上游
      kb_ingest 承担——桩 ``rerank`` 直通，不重复调用外部服务；
    - **熔断**：内层 ``rerank_documents`` 熔断打开时保序 fail-open
      （既有实现，见 agentix/retrieval/infra/rag/rerank.py）。
    """

    def __init__(self, enabled: Optional[bool] = None) -> None:
        if enabled is None:
            from agentix.retrieval.settings import settings

            enabled = bool(settings.RERANKER_ENABLED)
        self.enabled = enabled

    async def rerank(self, *args: Any, **kwargs: Any):
        return args[1] if len(args) > 1 else []


class KnowledgeGateway:
    """检索能力域网关（非弃用）：编排层 → RetrievalPortAdapter。"""

    def __init__(self, *, kb_id: Optional[str] = None) -> None:
        from agentix.retrieval.usecases.retrieval_port_adapter import RetrievalPortAdapter

        self._kb_id = kb_id or os.getenv("LEGELBOT_DEFAULT_KB_ID", "default")
        self._tenant_id = os.getenv("LEGELBOT_DEFAULT_TENANT_ID", "default")
        self._adapter = RetrievalPortAdapter()
        self.reranker = _RerankerCompatStub()
        logger.info("KnowledgeGateway initialised (kb_id=%s)", self._kb_id)

    @property
    def kb_id(self) -> str:
        return self._kb_id

    async def search(
        self,
        query: str,
        *,
        top_k: Optional[int] = None,
        similarity_threshold: Optional[float] = None,
        filter_expr: Optional[str] = None,
        filter_by_similarity: bool = True,
    ) -> List[Dict[str, Any]]:
        result = await self._adapter.search(
            query=query,
            kb_id=self._kb_id,
            tenant_id=self._tenant_id,
            top_k=top_k or 5,
        )
        if result["status"] != "ok":
            return []
        return [
            {
                "content": chunk["content"],
                "score": chunk["score"],
                "chunk_id": chunk["chunk_id"],
                "doc_id": chunk["citation"]["doc_id"],
                "metadata": {
                    "chunk_id": chunk["chunk_id"],
                    "document_id": chunk["citation"]["doc_id"],
                    "source": chunk["citation"]["doc_title"],
                    "page": chunk["citation"]["page"],
                    "kb_id": chunk["citation"]["kb_id"],
                    "tenant_id": chunk["citation"]["tenant_id"],
                },
            }
            for chunk in result["chunks"]
        ]

    async def delete_document(self, doc_id: str) -> bool:
        await self._adapter.delete_by_doc(doc_id=doc_id, tenant_id=self._tenant_id)
        return True

    async def get_stats(self) -> Dict[str, Any]:
        status = await self._adapter.index_status(kb_id=self._kb_id, tenant_id=self._tenant_id)
        return {
            "kb_id": status["kb_id"],
            "index_status": status["status"],
            "progress": status["progress"],
            "error_message": status["error_message"],
        }

    async def ingest_text(self, text: str, *, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        raise NotImplementedError(
            "Direct text ingestion is not supported by KnowledgeGateway. "
            "Use the retrieval domain upload API (/api/v1/retrieval/documents)."
        )

    async def add_document(
        self,
        *,
        doc_id: Optional[str],
        title: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        raise NotImplementedError(
            "Direct document ingestion is not supported by KnowledgeGateway. "
            "Use the retrieval domain upload API (/api/v1/retrieval/documents)."
        )

    async def clear(self) -> bool:
        raise NotImplementedError("clear is not supported by KnowledgeGateway.")

    async def close(self) -> None:
        pass
