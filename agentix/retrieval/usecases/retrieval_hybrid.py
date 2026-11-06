"""项目唯一混合检索链：rewrite → dense/sparse → z-score fusion。

默认由 ``RETRIEVAL_HYBRID_ENABLED=true`` 时被 ``retrieve_scoped_docs_cached``
调用；可通过显式关闭总闸回退至原 Dense 路径。

阶段职责：

- **rewrite**：问题归一（计时；由调用方传入 ``rewrite_ms``）；
- **dense**：既有 ``retrieve_docs_with_retry`` 向量路（fetch_k/阈值/有效性过滤口径不变，
  R-P1-4：分通道阈值在各自通道内生效）；
- **sparse**（R-P1-1，子闸 ``RETRIEVAL_SPARSE_ENABLED``）：进程内 BM25，
  语料 = PostgreSQL chunk 表；无 session 或异常时 fail-open 跳过；
- **dense / sparse 并行**：两路候选相互独立时并发执行，融合仍等待两路完成；
- **fusion**：z-score 归一 dense/BM25 分数后融合，Dense 权重由
  ``RETRIEVAL_FUSION_ALPHA`` 固定配置，候选池受 ``RETRIEVAL_FUSION_TOP_N`` 限制。

观测（R-P1-5）：阶段报告落 ContextVar（SSE 在预检后取走阶段帧），
同时经 ``span("rag.hybrid_stages", **attrs)`` 写入 trajectory（入口 kwargs）
与结构化日志，供评测归因（每阶段耗时/命中数/TopK id）。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from contextvars import ContextVar
from typing import Any, Dict, List, Optional, Tuple

from agentix.infrastructure.core.logger import get_logger
from agentix.retrieval.settings import settings

logger = get_logger(service="retrieval-hybrid")

#: SSE 侧（R-P1-5）在检索预检之后取走本请求的阶段报告。
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


def _topk_ids(docs: List[Any], limit: Optional[int] = None) -> str:
    n = int(limit if limit is not None else settings.RETRIEVAL_STAGE_TOPK_IDS)
    return ",".join(_chunk_key(d) for d in docs[: max(0, n)])


def _fused_meta_score(doc: Any) -> float:
    """zscore 口径下把 dense 分数换向为「越大越好」。"""
    meta = getattr(doc, "metadata", None) or {}
    raw = float(meta.get("_retrieval_score", 0.0) or 0.0)
    if settings.INDEX_SCORE_IS_SIMILARITY:
        return raw
    return -raw  # 距离口径：越小越相关 → 取负


def apply_fusion_min_score(
    docs: List[Any], *, min_score: Optional[float], fused: bool
) -> Tuple[List[Any], Dict[str, Any]]:
    """融合后分数闸：只在**双通道真融合**（z-score 口径）时生效。

    单通道保留的是 dense 距离原分（越小越好）或 BM25 原分，量纲与方向都不同，
    拿同一道下限过滤会反向误杀，故 ``fused=False`` 一律原样返回。
    过滤后为空不制造空答：fail-open 回退原序 Top1。
    """
    stats: Dict[str, Any] = {"applied": False, "dropped": 0, "fallback_top1": False}
    if not fused or min_score is None or not docs:
        return list(docs), stats
    kept = []
    for doc in docs:
        meta = getattr(doc, "metadata", None) or {}
        try:
            score = float(meta.get("_retrieval_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        if score >= float(min_score):
            kept.append(doc)
    stats["applied"] = True
    stats["dropped"] = len(docs) - len(kept)
    if not kept:
        stats["fallback_top1"] = True
        return list(docs[:1]), stats
    return kept, stats


async def rerank_fused_docs(
    docs: List[Any], *, question: str
) -> Tuple[Optional[List[Any]], Dict[str, Any]]:
    """精排融合候选；任何失败/超时/空结果返回 ``(None, stats)`` 由调用方保留原序。

    ``metadata`` 写入 ``_rerank_score``（精排分，越大越相关）、把原融合分留痕为
    ``_fusion_score``，并把 ``_retrieval_score`` 换成精排分供下游排序/归因复用。
    """
    from agentix.retrieval.infra.rag.rerank import (
        rerank_candidates,
        resolve_endpoint,
    )

    started = time.perf_counter()
    max_candidates = max(1, int(settings.RETRIEVAL_RERANK_MAX_CANDIDATES))
    top_n = max(1, int(settings.RETRIEVAL_RERANK_TOP_N))
    candidates = list(docs[:max_candidates])
    stats: Dict[str, Any] = {
        "applied": False,
        "candidates": len(candidates),
        "ms": 0.0,
        "skipped": None,
        "dropped": 0,
    }

    def _finish() -> Tuple[Optional[List[Any]], Dict[str, Any]]:
        stats["ms"] = (time.perf_counter() - started) * 1000.0
        return None, stats

    endpoint = resolve_endpoint(
        settings.RETRIEVAL_RERANK_BASE_URL, settings.RETRIEVAL_RERANK_ENDPOINT
    )
    if not endpoint:
        stats["skipped"] = "no_endpoint"
        return _finish()
    # 模板占位符/空 key 直接快速跳过：不发起注定 401 的请求，也不白等一个超时窗口。
    if settings.RETRIEVAL_RERANK_REQUIRE_API_KEY:
        key = (settings.RETRIEVAL_RERANK_API_KEY or "").strip()
        pattern = settings.RETRIEVAL_RERANK_PLACEHOLDER_PATTERN
        if not key or (pattern and re.search(pattern, key, flags=re.IGNORECASE)):
            stats["skipped"] = "no_credentials"
            return _finish()
    min_candidates = max(1, int(settings.RETRIEVAL_RERANK_MIN_CANDIDATES))
    if len(candidates) < min_candidates:
        stats["skipped"] = "too_few_candidates"
        return _finish()

    max_chars = max(1, int(settings.RETRIEVAL_RERANK_MAX_DOC_CHARS))
    texts = [(getattr(d, "page_content", "") or "")[:max_chars] for d in candidates]
    # 让服务多返回一批，min_score 裁完仍有余量凑满 top_n
    multiplier = max(1, int(settings.RETRIEVAL_RERANK_SERVICE_TOP_N_MULTIPLIER))
    service_min = max(1, int(settings.RETRIEVAL_RERANK_SERVICE_TOP_N_MIN))
    service_top_n = min(len(texts), max(top_n * multiplier, service_min))
    try:
        paired = await asyncio.to_thread(
            rerank_candidates,
            query=question,
            texts=texts,
            endpoint=endpoint,
            api_key=settings.RETRIEVAL_RERANK_API_KEY,
            model=settings.RETRIEVAL_RERANK_MODEL,
            top_n=service_top_n,
            timeout_seconds=float(settings.RETRIEVAL_RERANK_TIMEOUT_SECONDS),
        )
    except Exception as exc:  # noqa: BLE001 - 精排绝不打挂检索链
        logger.warning("rerank task failed (fail-open): {}", exc)
        stats["skipped"] = f"error:{type(exc).__name__}"
        return _finish()
    if paired is None:
        stats["skipped"] = "unavailable"
        return _finish()

    kept: List[Any] = []
    min_score = float(settings.RETRIEVAL_RERANK_MIN_SCORE)
    for idx, score in paired:
        if score < min_score:
            continue
        doc = candidates[idx]
        meta = dict(getattr(doc, "metadata", None) or {})
        meta["_fusion_score"] = float(meta.get("_retrieval_score", 0.0) or 0.0)
        meta["_rerank_score"] = float(score)
        meta["_retrieval_score"] = float(score)
        doc.metadata = meta
        kept.append(doc)
        if len(kept) >= top_n:
            break
    stats["dropped"] = len(candidates) - len(kept)
    if not kept:
        stats["skipped"] = "min_score_filtered_all"
        return _finish()
    stats["applied"] = True
    stats["ms"] = (time.perf_counter() - started) * 1000.0
    return kept, stats


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
    """混合检索并返回 ``(docs, stage_report)``。

    ``docs`` 与 flag-off 路径同契约：langchain Document 列表，
    ``metadata["_retrieval_score"]`` 已写入（单通道为原分，双通道为 z-score 融合分）。
    """
    # 懒加载：chat_service 在其内部懒加载本模块（避免 usecases 环状顶层 import）
    from agentix.retrieval.usecases.chat_service import retrieve_docs_with_retry

    top_n_cap = max(1, int(settings.RETRIEVAL_FUSION_TOP_N))
    channel_mult = max(1, int(settings.RETRIEVAL_CHANNEL_MULTIPLIER))
    channel_n = max(int(search_k), min(top_n_cap, int(search_k) * channel_mult))
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
        # 仍不返回才按空结果降级（下游 z-score 拿 dense 单路继续走）。
        timeout = float(settings.RETRIEVAL_HYBRID_GATHER_TIMEOUT_SECONDS)
        if timeout <= 0:
            timeout = float(settings.RETRIEVAL_HYBRID_GATHER_TIMEOUT_FALLBACK_SECONDS)
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

    # --- 唯一分数融合策略：Dense + BM25/z-score ---
    t0 = time.perf_counter()
    from agentix.domain.policies.rank_fusion import zscore_fuse

    sparse_docs = [d for d, _ in sparse_pairs]
    fused: List[Tuple[str, float]] = []
    fused_only = bool(dense_docs and sparse_docs)
    if fused_only:
        dense_channel = [(_chunk_key(d), _fused_meta_score(d)) for d in dense_docs]
        sparse_channel = [(_chunk_key(doc), float(s)) for doc, s in sparse_pairs]
        fused = zscore_fuse(
            [dense_channel, sparse_channel],
            alpha=float(settings.RETRIEVAL_FUSION_ALPHA),
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
            method="zscore",
        )
    )

    # --- R-P4 融合后质量闸（默认关：min_score 未设置 ⇒ 逐位等价）---
    gate_stats: Dict[str, Any] = {}
    if settings.RETRIEVAL_FUSION_MIN_SCORE is not None:
        ordered, gate_stats = apply_fusion_min_score(
            ordered,
            min_score=settings.RETRIEVAL_FUSION_MIN_SCORE,
            fused=fused_only,
        )
        if gate_stats.get("applied"):
            stages.append(
                _stage(
                    "fusion_gate",
                    0.0,
                    dropped=gate_stats["dropped"],
                    fallback_top1=gate_stats["fallback_top1"],
                )
            )

    # --- R-P4 精排（默认关：flag-off 一段帧都不加）---
    rerank_stats: Dict[str, Any] = {}
    if settings.RETRIEVAL_RERANK_ENABLED:
        reranked, rerank_stats = await rerank_fused_docs(
            ordered, question=question or question_norm
        )
        if reranked is not None:
            ordered = reranked
        stages.append(
            _stage(
                "rerank",
                float(rerank_stats.get("ms", 0.0)),
                hits=len(ordered),
                applied=bool(rerank_stats.get("applied")),
                candidates=int(rerank_stats.get("candidates", 0)),
                skipped=rerank_stats.get("skipped"),
            )
        )

    docs = ordered[: max(1, int(search_k))]
    report: Dict[str, Any] = {
        "stages": stages,
        "fusion_method": "zscore",
        "sparse": sparse_skipped or ("on" if settings.RETRIEVAL_SPARSE_ENABLED else "off"),
        "docs_count": len(docs),
    }
    if gate_stats.get("applied"):
        report["fusion_gate"] = gate_stats
    if rerank_stats:
        report["rerank"] = rerank_stats

    # R-P1-5 归因：trajectory span（入口 kwargs 进轨迹）+ 结构化日志；观测绝不打挂检索
    try:
        from agentix.observability.spans import span

        attrs: Dict[str, Any] = {
            "kb_id": kb_id,
            "search_k": int(search_k),
            "fusion_method": "zscore",
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
        "hybrid retrieval kb={} dense={} sparse={} fused={} docs={}",
        kb_id,
        len(dense_docs),
        report["sparse"],
        len(ordered),
        len(docs),
    )
    return docs, report
