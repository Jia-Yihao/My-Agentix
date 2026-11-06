"""R-P1（F1a）混合检索四阶段编排：rewrite → dense/sparse → fusion → rerank。

默认由 ``RETRIEVAL_HYBRID_ENABLED=true`` 时被 ``retrieve_scoped_docs_cached``
调用；可通过显式关闭总闸回退至原 Dense 路径。

阶段职责（检索增强深度优化.md §5）：

- **rewrite**：问题归一（计时；由调用方传入 ``rewrite_ms``）；
- **dense**：既有 ``retrieve_docs_with_retry`` 向量路（fetch_k/阈值/有效性过滤口径不变，
  R-P1-4：分通道阈值在各自通道内生效）；
- **sparse**（R-P1-1，子闸 ``RETRIEVAL_SPARSE_ENABLED``）：进程内 BM25，
  语料 = PostgreSQL chunk 表；无 session 或异常时 fail-open 跳过；
- **dense / sparse 并行**：两路候选相互独立时并发执行，融合仍等待两路完成；
- **fusion**（R-P1-2）：rank 级 RRF 或 z-score 分数融合
  （``RETRIEVAL_FUSION_METHOD`` = rrf|zscore；默认 zscore、Dense 权重 0.85；
  ``RETRIEVAL_FUSION_TOP_N`` 截断候选池）——融合后**不再**
  复用 dense 的距离/相似度阈值（RRF 分数与向量分数不同口径），只按 TopN 截断；
- **rerank**（R-P1-3）：``RETRIEVAL_HYBRID_RERANK=true``（默认关，需显式启用）时
  对 ``RETRIEVAL_RERANK_CANDIDATES`` 候选精排取 TopK；服务失败/熔断开路 =
  既有 fail-open（保序返回），熔断位记入阶段报告。

观测（R-P1-5）：阶段报告落 ContextVar（SSE 在预检后取走产四阶段帧），
同时经 ``span("rag.hybrid_stages", **attrs)`` 写入 trajectory（入口 kwargs）
与结构化日志，供评测归因（每阶段耗时/命中数/TopK id/熔断位）。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from contextvars import ContextVar
from typing import Any, Dict, List, Optional, Tuple

from agentix.infrastructure.core.logger import get_logger
from agentix.retrieval.settings import settings

logger = get_logger(service="retrieval-hybrid")

#: SSE 侧（R-P1-5）在检索预检之后取走本请求的四阶段报告。
_STAGE_REPORT: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    "agentix_retrieval_stage_report", default=None
)


def set_stage_report(report: Optional[Dict[str, Any]]) -> None:
    _STAGE_REPORT.set(report)


def pop_stage_report() -> Optional[Dict[str, Any]]:
    """取走并清空当前上下文的阶段报告（幂等：重复取为 None）。"""
    report = _STAGE_REPORT.get()
    _STAGE_REPORT.set(None)
    return report


def _stage(name: str, ms: float, **extra: Any) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"stage": name, "ms": round(float(ms), 3)}
    entry.update(extra)
    return entry


def _chunk_key(doc: Any) -> str:
    """跨通道去重键：chunk_id 同源优先，缺失时回退内容哈希。"""
    meta = getattr(doc, "metadata", None) or {}
    chunk_id = meta.get("chunk_id")
    if chunk_id:
        return str(chunk_id)
    text = getattr(doc, "page_content", "") or ""
    return "c:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _topk_ids(docs: List[Any], limit: int = 10) -> str:
    return ",".join(_chunk_key(d) for d in docs[:limit])


def _rerank_breaker_state() -> str:
    """读 rerank 熔断位（R-P1-5 归因供数）；读取失败记 unknown，绝不抛出。"""
    try:
        from agentix.reliability.circuit_breaker import registry

        breaker = registry.get("rerank_service")
        if breaker is None:
            return "none"
        state = breaker.state
        return str(getattr(state, "value", state)).lower()
    except Exception:  # noqa: BLE001 - 观测绝不打挂检索
        return "unknown"


def _fused_meta_score(doc: Any) -> float:
    """zscore 口径下把 dense 分数换向为「越大越好」。"""
    meta = getattr(doc, "metadata", None) or {}
    raw = float(meta.get("_retrieval_score", 0.0) or 0.0)
    if settings.INDEX_SCORE_IS_SIMILARITY:
        return raw
    return -raw  # 距离口径：越小越相关 → 取负


async def retrieve_hybrid_docs(
    *,
    kb_id: str,
    question: str,
    question_norm: str,
    search_k: int,
    metadata_filter: Any,
    vectorstore: Any,
    artifact_id: str,
    rewrite_ms: float,
    session: Any = None,
) -> Tuple[List[Any], Dict[str, Any]]:
    """四阶段混合检索，返回 ``(docs, stage_report)``。

    ``docs`` 与 flag-off 路径同契约：langchain Document 列表，
    ``metadata["_retrieval_score"]`` 已写入（单通道 = 各通道原分；融合 = RRF/z-score 融合分）。
    """
    # 懒加载：chat_service 在其内部懒加载本模块（避免 usecases 环状顶层 import）
    from agentix.retrieval.usecases.chat_service import retrieve_docs_with_retry

    top_n_cap = max(1, int(settings.RETRIEVAL_FUSION_TOP_N))
    channel_n = max(int(search_k), min(top_n_cap, int(search_k) * 4))
    stages: List[Dict[str, Any]] = [_stage("rewrite", rewrite_ms)]

    async def _retrieve_dense() -> Tuple[List[Any], Dict[str, Any]]:
        started = time.perf_counter()
        docs = await asyncio.to_thread(
            retrieve_docs_with_retry,
            vectorstore,
            question_norm,
            search_k=channel_n,
            metadata_filter=metadata_filter,
        )
        return docs, _stage(
            "dense",
            (time.perf_counter() - started) * 1000.0,
            hits=len(docs),
            topk=_topk_ids(docs),
        )

    async def _retrieve_sparse() -> Tuple[List[Tuple[Any, float]], Dict[str, Any]]:
        from agentix.domain.policies.legal_effectiveness import filter_in_force
        from agentix.retrieval.infra.rag.sparse import sparse_search

        started = time.perf_counter()
        pairs = await asyncio.to_thread(
            sparse_search,
            session=session,
            kb_id=kb_id,
            query=question_norm,
            top_n=channel_n,
            metadata_filter=metadata_filter,
            min_score=float(settings.RETRIEVAL_SPARSE_MIN_SCORE),
            ttl_seconds=int(settings.RETRIEVAL_SPARSE_CACHE_TTL),
            artifact_id=artifact_id,
        )
        pairs = list(filter_in_force(pairs, enabled=settings.EFFECTIVENESS_FILTER_ENABLED))
        return pairs, _stage(
            "sparse",
            (time.perf_counter() - started) * 1000.0,
            hits=len(pairs),
            topk=_topk_ids([doc for doc, _ in pairs]),
        )

    dense_task = asyncio.create_task(_retrieve_dense())
    sparse_pairs: List[Tuple[Any, float]] = []
    sparse_skipped: Optional[str] = None
    sparse_task = None
    if settings.RETRIEVAL_SPARSE_ENABLED:
        if session is None:
            sparse_skipped = "no_session"
            logger.warning("hybrid sparse skipped: no session provided (kb={})", kb_id)
        else:
            sparse_task = asyncio.create_task(_retrieve_sparse())

    if sparse_task is None:
        dense_docs, dense_stage = await dense_task
        sparse_stage = _stage(
            "sparse",
            0.0,
            hits=0,
            skipped=sparse_skipped or "disabled",
        )
    else:
        # 整体 timeout + return_exceptions=True：任一路慢/异常都不许把整链挂死。
        # sparse 是可旁路通道，超时先掐它；dense 是主通道，再等一个同长窗口，
        # 仍不返回才按空结果降级（下游 RRF 拿 dense 单路继续走）。
        timeout = float(settings.RETRIEVAL_HYBRID_GATHER_TIMEOUT_SECONDS)
        if timeout <= 0:
            timeout = 5.0
        gathered = asyncio.gather(dense_task, sparse_task, return_exceptions=True)
        try:
            dense_result, sparse_result = await asyncio.wait_for(
                asyncio.shield(gathered), timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.warning("hybrid gather timed out after {:.1f}s; dropping sparse", timeout)
            if not sparse_task.done():
                sparse_task.cancel()
            sparse_result = asyncio.TimeoutError("hybrid gather timeout")
            try:
                dense_result = await asyncio.wait_for(
                    asyncio.shield(dense_task), timeout=timeout
                )
            except Exception as exc:  # noqa: BLE001 - dense 也挂了：按空结果降级
                if not dense_task.done():
                    dense_task.cancel()
                dense_result = exc

        if isinstance(dense_result, BaseException):
            dense_docs, dense_stage = [], _stage(
                "dense", 0.0, hits=0, skipped=f"error:{type(dense_result).__name__}"
            )
        else:
            dense_docs, dense_stage = dense_result
        if isinstance(sparse_result, BaseException):
            sparse_pairs = []
            sparse_stage = _stage(
                "sparse", 0.0, hits=0, skipped=f"error:{type(sparse_result).__name__}"
            )
        else:
            sparse_pairs, sparse_stage = sparse_result
    stages.extend((dense_stage, sparse_stage))

    # --- 分数融合（R-P1-2） ---
    t0 = time.perf_counter()
    from agentix.domain.policies.rank_fusion import rrf_fuse, zscore_fuse

    method = settings.RETRIEVAL_FUSION_METHOD
    if method not in ("rrf", "zscore"):
        logger.warning(
            "unsupported retrieval fusion method {}; falling back to project default zscore",
            method,
        )
        method = "zscore"
    sparse_docs = [d for d, _ in sparse_pairs]
    fused: List[Tuple[str, float]] = []
    if dense_docs and sparse_docs:
        if method == "zscore":
            dense_channel = [(_chunk_key(d), _fused_meta_score(d)) for d in dense_docs]
            sparse_channel = [(_chunk_key(doc), float(s)) for doc, s in sparse_pairs]
            fused = zscore_fuse(
                [dense_channel, sparse_channel],
                alpha=float(settings.RETRIEVAL_FUSION_ALPHA),
                top_n=top_n_cap,
            )
        else:
            fused = rrf_fuse(
                [[_chunk_key(d) for d in dense_docs], [_chunk_key(d) for d in sparse_docs]],
                k=int(settings.RETRIEVAL_RRF_K),
                top_n=top_n_cap,
            )
        # dense 文档优先（索引 metadata 更全），仅有 sparse 命中的用 sparse 文档
        doc_by_key: Dict[str, Any] = {}
        for d in sparse_docs:
            doc_by_key.setdefault(_chunk_key(d), d)
        for d in dense_docs:
            doc_by_key[_chunk_key(d)] = d
        ordered = [doc_by_key[k] for k, _ in fused if k in doc_by_key]
        fused_score = dict(fused)
        for d in ordered:
            meta = dict(getattr(d, "metadata", None) or {})
            meta["_retrieval_score"] = float(
                fused_score.get(_chunk_key(d), meta.get("_retrieval_score", 0.0))
            )
            d.metadata = meta
    elif dense_docs:
        # 单通道（sparse 关/跳过/空）：保持 dense 原分与顺序（不写融合分）
        ordered = list(dense_docs)
        fused = [
            (_chunk_key(d), float((d.metadata or {}).get("_retrieval_score", 0.0) or 0.0))
            for d in dense_docs
        ]
    else:
        ordered = list(sparse_docs)
        fused = [(_chunk_key(d), float(s)) for _, s in sparse_pairs]
    ordered = ordered[:top_n_cap]
    stages.append(
        _stage(
            "fusion",
            (time.perf_counter() - t0) * 1000.0,
            hits=len(ordered),
            method=method,
        )
    )

    # --- rerank（R-P1-3：hybrid 链内默认开；失败/熔断 = 既有 fail-open 保序；
    #     W2 动态跳过（RERANK_DYNAMIC_SKIP_ENABLED，默认关）：候选过少/首位分
    #     已高 ⇒ 保序直过，stage 留痕 applied=false + skipped=原因） ---
    t0 = time.perf_counter()
    breaker = "disabled"
    rerank_skip: Optional[str] = None
    if settings.RETRIEVAL_HYBRID_RERANK and len(ordered) >= 2:
        from agentix.retrieval.infra.rag.rerank import dynamic_rerank_skip_reason

        rerank_skip = dynamic_rerank_skip_reason(ordered)
    if rerank_skip is None and settings.RETRIEVAL_HYBRID_RERANK and len(ordered) >= 2:
        from agentix.retrieval.infra.rag.rerank import (
            rerank_documents_with_status,
        )

        candidate_n = max(2, int(settings.RETRIEVAL_RERANK_CANDIDATES))
        candidates = ordered[:candidate_n]
        try:
            try:
                ordered, rerank_applied = await rerank_documents_with_status(
                    question, candidates, getattr(settings, "RERANKER_MODEL", None)
                )
            except Exception as exc:  # noqa: BLE001 - 双保险 fail-open（rerank_documents 内部已兜底）
                logger.warning("hybrid rerank degraded (fail-open): {}", exc)
                ordered = candidates
                rerank_applied = False
        finally:
            breaker = _rerank_breaker_state()
        stages.append(
            _stage(
                "rerank",
                (time.perf_counter() - t0) * 1000.0,
                hits=len(candidates),
                breaker=breaker,
                applied=rerank_applied,
            )
        )
    else:
        skip_extra: Dict[str, Any] = {}
        if rerank_skip:
            # 动态跳过：如实记录观测态（不触上游 ⇒ breaker 取注册表现状）
            breaker = _rerank_breaker_state()
            skip_extra["skipped"] = rerank_skip
        stages.append(
            _stage(
                "rerank",
                0.0,
                hits=0,
                breaker=breaker,
                applied=False,
                **skip_extra,
            )
        )

    docs = ordered[: max(1, int(search_k))]
    report: Dict[str, Any] = {
        "stages": stages,
        "fusion_method": method,
        "sparse": sparse_skipped or ("on" if settings.RETRIEVAL_SPARSE_ENABLED else "off"),
        "docs_count": len(docs),
    }

    # R-P1-5 归因：trajectory span（入口 kwargs 进轨迹）+ 结构化日志；观测绝不打挂检索
    try:
        from agentix.observability.spans import span

        attrs: Dict[str, Any] = {
            "kb_id": kb_id,
            "search_k": int(search_k),
            "fusion_method": method,
            "rerank_breaker": breaker,
            "docs_count": len(docs),
        }
        for st in stages:
            name = st["stage"]
            attrs[f"stage_{name}_ms"] = st["ms"]
            if "hits" in st:
                attrs[f"stage_{name}_hits"] = int(st["hits"])
            if st.get("topk"):
                attrs[f"stage_{name}_topk"] = st["topk"]
            if "skipped" in st:
                attrs[f"stage_{name}_skipped"] = str(st["skipped"])
        async with span("rag.hybrid_stages", **attrs):
            pass
    except Exception as exc:  # noqa: BLE001 - 观测失败不影响已成功的检索
        logger.debug("hybrid stage span degraded: {}", exc)

    logger.info(
        "hybrid retrieval kb={} dense={} sparse={} fused={} rerank={} docs={}",
        kb_id,
        len(dense_docs),
        report["sparse"],
        len(ordered),
        breaker,
        len(docs),
    )
    return docs, report
