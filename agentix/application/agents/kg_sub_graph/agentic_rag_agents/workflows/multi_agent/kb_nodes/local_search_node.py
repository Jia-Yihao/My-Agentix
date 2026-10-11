"""KB local_search 节点（P1-1 拆分，从 multi_tool.py 搬移；P1-5 并发化）。

本地检索策略：PostgreSQL 与 Milvus 两路并发（见 kb_retrieval.hybrid_retrieve），
`local_results` 对外默认保持「postgres 优先、milvus 兜底」的向后兼容语义，
并新增 `degraded` 标记透传部分失败信息。

R-P1-2（Channel B，F1a）：``KB_LOCAL_FUSION_ENABLED=true``（默认关）时改为
**跨通道 rank 融合**（rrf/zscore 可配，域内纯函数见
``agentix.domain.policies.rank_fusion``），替代「postgres 非空即丢掉 milvus」
的丢分逻辑；flag-off 语义逐位不变。
"""

import hashlib
from typing import Any, Dict, List

from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_retrieval import (
    hybrid_retrieve,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_states import (
    KBWorkflowConfig,
    KBWorkflowState,
)
from agentix.config.settings import settings
from agentix.domain.policies.rank_fusion import rrf_fuse, zscore_fuse
from agentix.infrastructure.core.logger import get_logger
from agentix.application.services.knowledge_gateway import KnowledgeGateway

kb_logger = get_logger(service="kb-local-search-node")


def _fusion_key(item: Dict[str, Any], channel: str, idx: int) -> str:
    """跨通道去重键：内容哈希优先（两通道 id 空间不同），id 次之，兜底通道序号。"""
    content = (item.get("content") or item.get("document") or "").strip()
    if content:
        return "c:" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:24]
    item_id = item.get("id")
    if item_id:
        return str(item_id)
    return f"{channel}:{idx}"


def fuse_local_channels(
    postgres_results: List[Dict[str, Any]],
    milvus_results: List[Dict[str, Any]],
    *,
    top_k: int,
) -> List[Dict[str, Any]]:
    """两路 rank 融合（R-P1-2 Channel B）。同内容重复取 postgres 侧条目（结构化字段更全）。"""
    method = (settings.KB_LOCAL_FUSION_METHOD or "rrf").strip().lower()
    pg_keys = [_fusion_key(item, "pg", i) for i, item in enumerate(postgres_results)]
    mv_keys = [_fusion_key(item, "mv", i) for i, item in enumerate(milvus_results)]
    if method == "zscore":
        # 分数方向约定「越大越好」：postgres 用 rerank/相似度，milvus 用 score
        def scores(keys: List[str], items: List[Dict[str, Any]]) -> List[tuple]:
            out = []
            for key, item in zip(keys, items):
                raw = item.get("rerank_score")
                if raw is None:
                    raw = item.get("similarity")
                if raw is None:
                    raw = item.get("score", 0.0)
                try:
                    out.append((key, float(raw or 0.0)))
                except (TypeError, ValueError):
                    out.append((key, 0.0))
            return out

        fused = zscore_fuse(
            [scores(pg_keys, postgres_results), scores(mv_keys, milvus_results)],
            alpha=0.5,
            top_n=max(1, top_k),
        )
    else:
        fused = rrf_fuse([pg_keys, mv_keys], k=60, top_n=max(1, top_k))

    by_key: Dict[str, Dict[str, Any]] = {}
    for i, item in enumerate(postgres_results):  # postgres 先占位（同键取 pg）
        by_key.setdefault(pg_keys[i], item)
    for i, item in enumerate(milvus_results):
        by_key.setdefault(mv_keys[i], item)
    return [by_key[key] for key, _ in fused if key in by_key]


def create_local_search_node(
    knowledge_service: KnowledgeGateway,
    cfg: KBWorkflowConfig,
):
    """构造 local_search 节点：并发查询 PostgreSQL 与 Milvus（P1-5 并发化）。"""

    async def local_search(state: KBWorkflowState) -> Dict[str, Any]:
        """
        并发执行本地检索：kb_ingest 结构化库（Milvus legal）与 Milvus 向量库两路并行下发。

        执行策略（P1-5）：
        1. 两路都被选中 → 并发执行，互不阻塞（T ≈ max(T_pg, T_milvus)）。
        2. `local_results` 默认保持 postgres 优先语义：postgres 非空 → 用 postgres；
           否则用 milvus（向后兼容）。
        3. 部分通道失败 → `degraded=True`；全部通道失败 → 抛 RETRIEVAL_FAILED。
        4. R-P1-2 Channel B：``KB_LOCAL_FUSION_ENABLED=true`` 且两路都非空时改走
           rank 融合（不再丢掉 milvus 分）；flag-off（默认）与旧行为逐位一致。
        """
        question = state.get("question", "")
        if not question.strip():
            return {
                "milvus_results": [],
                "postgres_results": [],
                "local_results": [],
                "steps": ["local_search"],
                "degraded": False,
            }

        selected_tools = state.get("kb_tools") or ["postgres", "milvus"]

        retrieval = await hybrid_retrieve(
            question,
            cfg=cfg,
            knowledge_service=knowledge_service,
            tools=selected_tools,
        )
        postgres_results = retrieval.get("postgres", []) or []
        milvus_results = retrieval.get("milvus", []) or []
        degraded = bool(retrieval.get("degraded", False))

        if (
            settings.KB_LOCAL_FUSION_ENABLED
            and postgres_results
            and milvus_results
        ):
            # R-P1-2：跨通道归一融合（默认 rrf 免调参），两路命中都保留
            local_results = fuse_local_channels(
                postgres_results, milvus_results, top_k=cfg.top_k
            )
            kb_logger.info(
                "✅ R-P1-2 双路融合（%s）：pg=%d mv=%d → %d",
                settings.KB_LOCAL_FUSION_METHOD,
                len(postgres_results),
                len(milvus_results),
                len(local_results),
            )
        elif postgres_results:
            # local_results 保持 postgres 优先语义（flag-off 向后兼容）
            kb_logger.info(
                "✅ PostgreSQL 有结果（{}条），直接使用结构化数据",
                len(postgres_results),
            )
            local_results = postgres_results
        else:
            local_results = milvus_results

        route = state.get("route", "local")
        if (
            not local_results
            and route in {"local", "hybrid"}
            and cfg.allow_external_search
            and cfg.external_url
        ):
            kb_logger.info("Local searches empty, falling back to external search.")
            route = "external"

        return {
            "milvus_results": milvus_results,
            "postgres_results": postgres_results,
            "local_results": local_results,
            "route": route,
            "steps": ["local_search"],
            "degraded": degraded,
        }

    return local_search
