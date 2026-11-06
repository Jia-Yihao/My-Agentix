"""进程内检索器缓存——消灭「每次检索都重建客户端 + 反序列化索引」。

背景（docs/已知限制.md「检索链路每请求重建 embedding 客户端」，两套栈实测）：请求路径上的每次检索都会走
``build_retriever → create_vector_store → FAISSVectorStore.load()``，其中
``create_embeddings()`` 会新建一个 OpenAI 兼容客户端。实测这段构造在仓库登记的
``.venv`` 上是 16–24ms（首次 259ms），而在另一套 SDK 栈上是 2053–4025ms
（cProfile 指向 ``_ssl._SSLContext.load_verify_locations``——每个 httpx client 装一次
系统 CA，而本地模型端点是明文 HTTP，这笔开销用不上）。同一份代码、同一份语料，
检索延迟差 23 倍，**逐题位次却完全一致**——所以这是「形状」问题，不是质量问题，
本模块只搬走构造，不动检索结果。

口径（必须逐条读）：
1. ``RETRIEVAL_RETRIEVER_CACHE_ENABLED`` 默认 **关**：:func:`get_or_build_retriever`
   每次都直调 ``build_retriever``，与接线前**逐位等价**（同一对象图、同一异常）。
2. 签名 = ``backend | abspath(index_path) | index.faiss(mtime_ns,size) |
   embedding_model | embedding_base_url | k``。索引重建（文件被重写）⇒ 签名变 ⇒
   自动失效，因此**没有** TTL/显式失效钩子——缓存的有效性只由磁盘事实决定。
3. ``index.faiss`` stat 失败（含不存在）⇒ 不走缓存，直调 ``build_retriever``：
   让 ``FileNotFoundError`` 的抛出时机与基线完全一致（上层靠它判 degraded）。
4. 构造持锁（single-flight）：并发首建只构造一次，其余请求等待后命中缓存。
   代价是首建期间同签名的检索被串行化——这是刻意的，避免 N 份索引同时驻留内存。
5. 进程内、进程私有：多 worker 各持一份（内存 ≈ 索引大小 × worker 数）。
   构造完成后 langchain/FAISS 的检索调用是只读的，可并发共用同一实例。
6. 调用方**不得**把返回的 vectorstore 当写入句柄（索引写入走
   ``index_worker.create_vector_store`` 那条独立路径，不经本缓存）。
"""

from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

# 签名 → (retriever, vectorstore)。OrderedDict 兼作 LRU（move_to_end / popitem）。
_cache: "OrderedDict[tuple, Tuple[Any, Any]]" = OrderedDict()
_lock = threading.Lock()


def _enabled() -> bool:
    from agentix.retrieval.settings import settings

    return bool(getattr(settings, "RETRIEVAL_RETRIEVER_CACHE_ENABLED", False))


def _max_entries() -> int:
    from agentix.retrieval.settings import settings

    raw = getattr(settings, "RETRIEVAL_RETRIEVER_CACHE_MAX_ENTRIES", 8)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):  # 配置写坏 ⇒ 回退默认 8，不打断检索
        return 8


def _signature(index_path: str) -> Optional[tuple]:
    """索引签名；stat 失败返回 None（调用方回退直构，异常语义与基线一致）。"""
    from agentix.retrieval.config import get_default_config
    from agentix.retrieval.settings import settings

    try:
        st = os.stat(os.path.join(str(index_path), "index.faiss"))
    except OSError:
        return None
    cfg = get_default_config()
    return (
        str(getattr(settings, "VECTOR_STORE_BACKEND", "faiss")),
        os.path.abspath(str(index_path)),
        int(st.st_mtime_ns),
        int(st.st_size),
        str(cfg.get("embedding_model") or ""),
        str(cfg.get("embedding_base_url") or ""),
        int(cfg.get("search_k", 5) or 5),
    )


def get_or_build_retriever(index_path: str) -> Tuple[Any, Any]:
    """``build_retriever`` 的缓存包装，返回 ``(retriever, vectorstore)``。

    flag 关（默认）⇒ 每次直调，行为与接线前逐位等价。
    """
    from agentix.retrieval.usecases.chat_service import build_retriever

    if not _enabled():
        return build_retriever(index_path)

    sig = _signature(index_path)
    if sig is None:
        return build_retriever(index_path)

    with _lock:
        hit = _cache.get(sig)
        if hit is not None:
            _cache.move_to_end(sig)
            return hit
        # 构造在锁内：并发首建只构造一次（见模块口径 4）。
        pair = build_retriever(index_path)
        _cache[sig] = pair
        _cache.move_to_end(sig)
        while len(_cache) > _max_entries():
            _cache.popitem(last=False)
        return pair


def reset_retriever_cache() -> None:
    """清空进程内缓存。仅测试隔离用；生产无调用点。"""
    with _lock:
        _cache.clear()


def cache_stats() -> dict:
    """只读观测：条目数与上限（测试/诊断用，不进 Prometheus 指标面）。"""
    with _lock:
        return {"entries": len(_cache), "max_entries": _max_entries()}