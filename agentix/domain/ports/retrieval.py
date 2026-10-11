"""检索能力域端口抽象（Protocol）。

依据《Tracerag-My Agentix 融合设计》§2.4.4 RetrievalPort 契约：
- 规则：``application`` 编排层仅经 ``RetrievalPort`` 触达检索能力域，
  禁止 ``application`` 包直接 ``import retrieval.infra``；
- 可抽取缝线：未来切换为远程 Adapter（HTTP/gRPC）时，
  ``domain`` 与 ``application`` 层零改动。

零第三方依赖（仅 stdlib + typing）——领域层不 import pymilvus / langchain /
sqlmodel，具体类型由 ``agentix.retrieval`` 侧的 Adapter 实现承担。

降级语义（design.md §2.4.2）：
- ``status="ok"``      — 正常命中
- ``status="empty"``   — 空库或无命中（低于阈值）
- ``status="degraded"`` — 检索能力域不可用（Milvus 超时 / worker 崩 / 索引未就绪）

安全约束：``tenant_id`` 由鉴权上下文注入，不信任客户端传值。
"""

from typing import List, Literal, Optional, Protocol, TypedDict, runtime_checkable

__all__ = [
    "RetrievalCitation",
    "RetrievalChunk",
    "RetrievalResult",
    "IndexStatus",
    "RetrievalPort",
]


class RetrievalCitation(TypedDict):
    """检索命中的引用元信息（design.md §2.4.2 Tool 输出 Schema）。"""

    kb_id: str
    doc_id: str
    doc_title: str
    page: Optional[int]
    tenant_id: str


class RetrievalChunk(TypedDict):
    """单条检索命中片段。"""

    chunk_id: str
    content: str
    score: float
    citation: RetrievalCitation


class RetrievalResult(TypedDict):
    """检索返回结构（design.md §2.4.2 Tool 输出 Schema）。"""

    status: Literal["ok", "empty", "degraded"]
    chunks: List[RetrievalChunk]
    error: Optional[str]


class IndexStatus(TypedDict):
    """知识库索引状态。"""

    kb_id: str
    status: Literal["ready", "indexing", "failed", "unknown"]
    progress: Optional[float]
    error_message: Optional[str]


@runtime_checkable
class RetrievalPort(Protocol):
    """检索能力域端口契约（design.md §2.4.4）。

    Agent 编排层（LangGraph 节点 / kb_tools）仅经此 Protocol 触达检索；
    本地 Adapter 实现位于 ``agentix.retrieval.usecases.retrieval_port_adapter``。
    """

    async def search(
        self,
        *,
        query: str,
        kb_id: str,
        tenant_id: str,
        top_k: int = 5,
    ) -> RetrievalResult:
        """语义检索：在指定知识库范围内返回 top-k 命中片段。

        降级时返回 ``status="degraded"`` 且 ``error`` 描述原因；
        空命中返回 ``status="empty"`` 且 ``chunks=[]``。
        ``tenant_id`` 为空/空白时实现必须 fail-closed（抛 ``ValueError``），
        不得以"无租户上下文"继续执行（R-P0-2）。
        """
        ...

    async def delete_by_doc(self, *, doc_id: str, tenant_id: str) -> None:
        """按文档 ID 删除该文档在向量库中的全部片段。

        ``tenant_id`` 为空时实现必须 fail-closed 抛 ``ValueError``（R-P0-2）。
        """
        ...

    async def index_status(self, *, kb_id: str, tenant_id: str) -> IndexStatus:
        """查询知识库当前索引状态（ready / indexing / failed / unknown）。

        ``tenant_id`` 为空时实现必须 fail-closed 抛 ``ValueError``（R-P0-2）。
        """
        ...
