"""Agent 检索结果重排适配器。

R1-3（依据《技术变更与架构演进指南 v2.0》§2.1.5③ 外部依赖熔断）：
- 显式 timeout（既有）+ **熔断**（新增）：rerank 服务连续失败达阈值后
  进入 OPEN，后续请求快速跳过重排（保留原召回顺序），不再每请求空耗
  一次超时等待；恢复超时后半开探测，成功即回 CLOSED。
- 降级语义（开闸降级）：跳过重排 = 按原序返回，答案质量下降但可用，
  与指南"跳过重排、只回本地、标注来源受限"一致。
"""

from __future__ import annotations

import os
import logging

import httpx

from agentix.retrieval.settings import settings

logger = logging.getLogger(__name__)
_RERANK_BREAKER_NAME = "rerank_service"

# Phase H·W2（RERANK_DYNAMIC_SKIP_ENABLED，默认关）：动态跳过阈值。
# 候选过少（≤4，再排也重不出花）或首位分已高（≥0.95，排序无损）⇒ 保序跳过。
DYNAMIC_SKIP_MAX_CANDIDATES = 4
DYNAMIC_SKIP_MIN_SCORE = 0.95


def dynamic_rerank_skip_reason(docs: list) -> str | None:
    """W2 精排动态跳过判定：返回跳过原因；flag-off 恒 ``None``（零行为变化）。

    - ``few_candidates``：候选数 ``<= DYNAMIC_SKIP_MAX_CANDIDATES``；
    - ``high_score``：首位归一化分 ``>= DYNAMIC_SKIP_MIN_SCORE``——**仅在
      相似度口径**（``INDEX_SCORE_IS_SIMILARITY``）下判定；距离/融合口径的
      ``_retrieval_score`` 语义不可比（距离越小越好、rrf 分数量级 ~1/k），
      保守不判（只做候选数跳过），绝不因口径误读把该排的序跳掉。
    """
    if not getattr(settings, "RERANK_DYNAMIC_SKIP_ENABLED", False):
        return None
    if len(docs) <= DYNAMIC_SKIP_MAX_CANDIDATES:
        return "few_candidates"
    if not getattr(settings, "INDEX_SCORE_IS_SIMILARITY", False):
        return None
    best: float | None = None
    for doc in docs:
        meta = getattr(doc, "metadata", None) or {}
        try:
            raw = float(meta.get("_retrieval_score"))
        except (TypeError, ValueError):
            continue
        if best is None or raw > best:
            best = raw
    if best is not None and best >= DYNAMIC_SKIP_MIN_SCORE:
        return "high_score"
    return None


async def _call_rerank(endpoint: str, payload: dict, docs: list) -> list:
    # Phase H · W3：SHARED_HTTP_MAX_CONNECTIONS>0 复用进程级共享 client
    # （连接池复用，超时经请求级 timeout 传入）；默认 0 = 每次新建关闭
    # （owns=True，请求零额外参数——与基线调用形状逐位一致）。
    from agentix.infrastructure.http import acquire_http_client

    timeout = settings.RERANKER_TIMEOUT_SECONDS
    async with acquire_http_client(timeout=timeout) as (client, owns):
        post_kwargs = {} if owns else {"timeout": timeout}
        response = await client.post(endpoint, json=payload, **post_kwargs)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("rerank response must be a JSON object")
        if body.get("error"):
            raise ValueError(f"rerank service returned an error: {body['error']}")
        rows = body.get("results")
        if not isinstance(rows, list) or not rows:
            raise ValueError("rerank response is missing non-empty results")
        if not all(isinstance(row, dict) and "index" in row for row in rows):
            raise ValueError("rerank results contain invalid rows")
    ranked = sorted(rows, key=lambda row: float(row.get("relevance_score", 0)), reverse=True)
    ordered = [docs[int(row["index"])] for row in ranked if 0 <= int(row["index"]) < len(docs)]
    if not ordered:
        raise ValueError("rerank response contains no valid document indices")
    return ordered


async def rerank_documents_with_status(
    query: str, docs: list, model: str | None = None
) -> tuple[list, bool]:
    """返回重排结果及是否实际成功；不可用时按原序 fail-open。"""
    if len(docs) < 2:
        return docs, False
    endpoint = (
        os.getenv(
            "RERANKER_BASE_URL", os.getenv("EMBEDDING_BASE_URL", "http://127.0.0.1:1234/v1")
        ).rstrip("/")
        + "/rerank"
    )
    payload = {
        "model": model or os.getenv("RERANKER_MODEL", "text-embedding-bge-reranker-v2-m3"),
        "query": query,
        "documents": [getattr(doc, "page_content", "") for doc in docs],
        "top_n": len(docs),
    }

    try:
        from agentix.reliability.circuit_breaker import (
            CircuitBreakerHalfOpenError,
            CircuitBreakerOpenError,
            registry as breaker_registry,
        )
        from agentix.retrieval.settings import settings as _rs

        breaker = breaker_registry.get_or_create(
            _RERANK_BREAKER_NAME,
            # 直接读声明字段（此前是 getattr(..., 字面量兜底)，而字段并不存在 ⇒ 环境变量
            # RERANK_BREAKER_* 永不被读到。现已在 retrieval/settings.py 正式声明，默认 5 / 60。）
            failure_threshold=max(1, int(_rs.RERANK_BREAKER_FAILURE_THRESHOLD)),
            recovery_timeout=float(_rs.RERANK_BREAKER_RECOVERY_SECONDS),
        )

        async def _call() -> list:
            return await _call_rerank(endpoint, payload, docs)

        return await breaker.call(_call), True
    except (CircuitBreakerOpenError, CircuitBreakerHalfOpenError):
        # 熔断打开/探测满：快速降级（原序返回），不发上游请求
        logger.info("rerank breaker open; serving original order")
        return docs, False
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        logger.warning("rerank degraded (fail-open): %s", exc)
        return docs, False


async def rerank_documents(query: str, docs: list, model: str | None = None) -> list:
    """调用兼容 /rerank 的服务；服务不可用时保留原召回顺序。"""
    ranked, _ = await rerank_documents_with_status(query, docs, model)
    return ranked


def reset_rerank_breaker_for_test() -> None:
    """重置熔断器单例（仅供测试）。"""
    from agentix.reliability.circuit_breaker import registry as breaker_registry

    breaker_registry.reset_all()
