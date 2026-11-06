"""Dense 候选池的「query 相关性记忆」重排接线（默认关）。

与 ``dense_rerank.py`` 同形：调用方先把候选池放大到
``RETRIEVAL_QUERY_MEMORY_CANDIDATES``，本模块重排后截回 ``search_k``；
记忆文件缺失、query 向量取不到、任一步异常 ⇒ fail-open 保留原召回顺序。

可选二次融合 ``RETRIEVAL_CE_FUSION_ENABLED``：在放大后的候选池头部取
Cross-Encoder 分数，按 ``z(记忆融合分) + μ·z(CE)`` 再排一次。CE 服务不可用
时同样 fail-open（跳过二次融合，不影响记忆重排结果）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np

from agentix.retrieval.settings import settings

logger = logging.getLogger(__name__)

_MEMORY_CACHE: dict[str, Any] = {}
_MEMORY_LOCK = threading.Lock()


def _load_memory():
    """进程内按 (path, mtime_ns, size) 缓存记忆快照；换文件自动失效。"""
    path = (settings.RETRIEVAL_QUERY_MEMORY_PATH or "").strip()
    if not path:
        return None
    p = Path(path)
    stat = p.stat()  # FileNotFoundError 由调用方 fail-open 捕获
    key = f"{p.resolve()}|{stat.st_mtime_ns}|{stat.st_size}"
    with _MEMORY_LOCK:
        hit = _MEMORY_CACHE.get(key)
        if hit is None:
            from agentix.retrieval.domain.policies.query_memory import load_query_memory

            hit = load_query_memory(p)
            _MEMORY_CACHE.clear()
            _MEMORY_CACHE[key] = hit
        return hit


def _query_vector(vectorstore: Any, question: str) -> np.ndarray | None:
    """从多种向量库实现里取 query embedding；取不到返回 None（fail-open）。"""
    embedders: list[Any] = []
    direct = getattr(vectorstore, "embeddings", None)
    if direct is not None:
        embedders.append(direct)
    inner = getattr(vectorstore, "store", None)
    get_emb = getattr(inner, "_get_embeddings", None)
    if callable(get_emb):
        try:
            embedders.append(get_emb())
        except Exception:  # noqa: BLE001 - 嵌入客户端构造失败按取不到处理
            pass
    own = getattr(vectorstore, "_embedding_model", None)
    if own is not None:
        embedders.append(own)
    for emb in embedders:
        if emb is None:
            continue
        fn = getattr(emb, "embed_query", None)
        if callable(fn):
            return np.asarray(fn(question), dtype=np.float32).reshape(-1)
        fn = getattr(emb, "embed_documents", None)
        if callable(fn):
            out = fn([question])
            if out:
                return np.asarray(out[0], dtype=np.float32).reshape(-1)
    return None


def _doc_metadata(doc: Any) -> dict:
    return dict(getattr(doc, "metadata", None) or {})


def _dense_similarity(meta: dict) -> float:
    """把检索分统一到「越大越相似」；距离口径按归一化 L2 → cos 换算。"""
    raw = float(meta.get("_retrieval_score", 0.0) or 0.0)
    if settings.INDEX_SCORE_IS_SIMILARITY:
        return raw
    return 1.0 - 0.5 * raw


def _ce_scores(question: str, docs: list, cand: int) -> list[float] | None:
    """调用 /rerank 拿候选头部逐条 relevance_score；失败返回 None。"""
    endpoint = (
        os.getenv(
            "RERANKER_BASE_URL", os.getenv("EMBEDDING_BASE_URL", "http://127.0.0.1:1234/v1")
        ).rstrip("/")
        + "/rerank"
    )
    head = docs[:cand]
    payload = json.dumps(
        {
            "model": os.getenv("RERANKER_MODEL", "text-embedding-bge-reranker-v2-m3"),
            "query": question,
            "documents": [str(getattr(d, "page_content", ""))[:512] for d in head],
            "top_n": len(head),
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=settings.RERANKER_TIMEOUT_SECONDS) as resp:
        body = json.load(resp)
    rows = body.get("results")
    if not isinstance(rows, list) or not rows:
        return None
    scores = [0.0] * len(head)
    for row in rows:
        i = int(row["index"])
        if 0 <= i < len(head):
            scores[i] = float(row.get("relevance_score", 0.0))
    return scores


async def rerank_docs_with_query_memory(
    question: str, docs: list, search_k: int, vectorstore: Any
) -> list:
    """重排并截到 ``search_k``；任一环节失败按原序截断（fail-open）。"""
    if not docs or len(docs) <= search_k:
        return list(docs[:search_k])
    try:
        from agentix.retrieval.domain.policies.query_memory import (
            fuse_ce_scores,
            rerank_with_query_memory,
        )

        memory = _load_memory()
        if memory is None:
            return list(docs[:search_k])
        qv = _query_vector(vectorstore, question)
        if qv is None or qv.size != memory.doc_vectors.shape[1]:
            return list(docs[:search_k])

        metas = [_doc_metadata(d) for d in docs]
        candidate_ids = [str(m.get("chunk_id", m.get("doc_id", ""))) for m in metas]
        dense = np.asarray([_dense_similarity(m) for m in metas], dtype=np.float32)
        doc_index = memory.doc_index
        dim = memory.doc_vectors.shape[1]
        vecs = np.zeros((len(docs), dim), dtype=np.float32)
        for i, cid in enumerate(candidate_ids):
            pos = doc_index.get(cid)
            if pos is not None:
                vecs[i] = memory.doc_vectors[pos]

        weights = {
            "wd": settings.RETRIEVAL_QUERY_MEMORY_WD,
            "wr": settings.RETRIEVAL_QUERY_MEMORY_WR,
            "wk": settings.RETRIEVAL_QUERY_MEMORY_WK,
            "roc_k": settings.RETRIEVAL_QUERY_MEMORY_ROC_K,
            "roc_beta": settings.RETRIEVAL_QUERY_MEMORY_ROC_BETA,
            "roc_power": settings.RETRIEVAL_QUERY_MEMORY_ROC_POWER,
            "knn_k": settings.RETRIEVAL_QUERY_MEMORY_KNN_K,
            "knn_power": settings.RETRIEVAL_QUERY_MEMORY_KNN_POWER,
        }
        order = rerank_with_query_memory(
            query_vector=qv,
            candidate_ids=candidate_ids,
            dense_scores=dense,
            candidate_vectors=vecs,
            memory=memory,
            weights=weights,
            top_k=search_k,
        )
        ordered = [docs[i] for i in order]

        if settings.RETRIEVAL_CE_FUSION_ENABLED:
            cand = min(len(ordered), max(search_k, int(settings.RETRIEVAL_RERANK_CANDIDATES)))
            ce = _ce_scores(question, ordered, cand)
            if ce is not None:
                roc_q = memory.rocchio_vector(
                    qv,
                    int(settings.RETRIEVAL_QUERY_MEMORY_ROC_K),
                    float(settings.RETRIEVAL_QUERY_MEMORY_ROC_BETA),
                    float(settings.RETRIEVAL_QUERY_MEMORY_ROC_POWER),
                )
                votes = memory.vote_scores(
                    qv,
                    int(settings.RETRIEVAL_QUERY_MEMORY_KNN_K),
                    float(settings.RETRIEVAL_QUERY_MEMORY_KNN_POWER),
                )
                head = ordered[:cand]
                head_ids = [candidate_ids[order[i]] for i in range(cand)]
                head_vecs = np.stack([vecs[order[i]] for i in range(cand)])
                head_dense = np.asarray([dense[order[i]] for i in range(cand)], dtype=np.float32)
                base = (
                    float(settings.RETRIEVAL_QUERY_MEMORY_WD) * head_dense
                    + float(settings.RETRIEVAL_QUERY_MEMORY_WR) * (head_vecs @ roc_q)
                    + float(settings.RETRIEVAL_QUERY_MEMORY_WK)
                    * np.asarray([votes.get(cid, 0.0) for cid in head_ids], dtype=np.float32)
                )
                fused = fuse_ce_scores(base, ce, float(settings.RETRIEVAL_CE_FUSION_MU))
                fused_order = np.argsort(-fused, kind="stable")
                ordered = [head[int(i)] for i in fused_order] + ordered[cand:]

        return list(ordered[:search_k])
    except Exception as exc:  # noqa: BLE001 - 记忆链路任何异常都按原召回顺序交付
        logger.warning("query memory rerank degraded (fail-open): %s", exc)
        return list(docs[:search_k])
