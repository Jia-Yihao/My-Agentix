"""Agent 检索结果重排适配器。

R1-3（依据《技术变更与架构演进指南 v2.0》§2.1.5③ 外部依赖熔断）：
- 显式 timeout（既有）+ **熔断**：rerank 服务连续失败达阈值后进入 OPEN，
  后续请求快速跳过重排（保留原召回顺序），不再每请求空耗一次超时等待；
  恢复超时后半开探测，成功即回 CLOSED。
- 降级语义（开闸降级）：跳过重排 = 按原序返回，答案质量下降但可用。

**只有一套重排实现**：候选按 ``batch_size`` 切连续批次，以 ``max_concurrency``
为上限并发打上游，全部批次的分数在全局按 ``relevance_score`` 降序融合（同分按
候选原始下标升序，确定性可复现）。``batch_size<=0`` 表示不切批——单批即全候选，
再叠加并发上限 1 就是「一次调用打完全部候选」的形态。此前并存的单次调用与串行
分批两条实现已并入本路径，不再保留副本，避免同一口径出现第二份代码。

超时单源：``RERANKER_TIMEOUT_SECONDS`` 既约束单批 HTTP 请求，也约束整次重排的
墙钟，不另开第二个超时键。

调用方：``rerank_documents_with_status``（hybrid 通道，读 ``RETRIEVAL_RERANK_BATCH_SIZE``）
与 ``rerank_documents_concurrent``（Dense 候选池，显式传批次 / 并发 / 超时）。
"""

from __future__ import annotations

import asyncio
import os
import logging
import time

import httpx

from agentix.retrieval.settings import settings

logger = logging.getLogger(__name__)
_RERANK_BREAKER_NAME = "rerank_service"
_DEFAULT_RERANK_MODEL = "text-embedding-bge-reranker-v2-m3"

# 上游异常判定集合——"什么算一次失败的上游调用"的唯一真源（约定 6 ④）：
# 分批路径的失败记账与外层 fail-open 捕获都用本常量，不得再另写字面量元组。
_RERANK_UPSTREAM_ERRORS = (httpx.HTTPError, KeyError, TypeError, ValueError)

# 一次重排尝试算失败的全部形态：上游异常 + 超时。单批请求与整体墙钟共用本常量。
# 注意 must be 扁平元组：except 不接受嵌套元组（``except (A, (B, C))`` 会在
# 求值 except 子句时抛 TypeError，把真正的失败盖成「捕获类非法」）。
_RERANK_FAILURES = (asyncio.TimeoutError,) + _RERANK_UPSTREAM_ERRORS

# 检索侧计数器（验收②/约定 3）。零 label ⇒ 基数恒为 1，天然不受
# tests/test_p100_genai_observability.py 的 ALLOWED_METRIC_LABELS 守卫影响——
# 该守卫实测枚举的是默认 REGISTRY 全部 collector 的 label 名（p100:284-305），
# 无 label 的指标不在它的对照面上，故不为其新增白名单项。
# prometheus_client 的导出真源是默认 REGISTRY
# （observability/metrics.get_metrics() → generate_latest()），任何模块注册的
# 指标都会自动出现在 /metrics，无需触碰集中定义文件。
# 降级口径与 observability/metrics.py 一致：prometheus_client 缺失时 noop。
try:
    from prometheus_client import Counter, Histogram

    RERANK_CALLS_TOTAL = Counter(
        "rerank_calls_total",
        "Upstream rerank calls issued (batched or not; breaker fast-fails issue no call and are not counted)",
    )
    RERANK_ERRORS_TOTAL = Counter(
        "rerank_errors_total",
        "Upstream rerank calls that failed (each failure degrades fail-open to the original order)",
    )
    RERANK_LATENCY_SECONDS = Histogram(
        "rerank_latency_seconds",
        "Upstream rerank call latency in seconds",
        # 桶形与检索域 rag_retrieval_duration_seconds 同刀（metrics.py:93-98）；
        # RERANKER_TIMEOUT_SECONDS 默认 10s，上界覆盖到超时点。
        buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0],
    )
except ImportError:  # pragma: no cover - 与 observability/metrics.py 同款兜底
    logger.warning("prometheus_client not installed; rerank counters degraded to noop")

    class _NoopMetric:
        def labels(self, *args, **kwargs):
            return self

        def inc(self, amount=1):
            pass

        def observe(self, value):
            pass

    RERANK_CALLS_TOTAL = _NoopMetric()
    RERANK_ERRORS_TOTAL = _NoopMetric()
    RERANK_LATENCY_SECONDS = _NoopMetric()


def _observe_rerank_call(duration_s: float, ok: bool) -> None:
    """记账一次上游调用（耗时 + 成败）。

    观测出错绝不影响主链检索（约定 2）：捕获后 warning 留痕并跳过——
    指标可以缺，rerank 结果不能炸。
    """
    try:
        RERANK_CALLS_TOTAL.inc()
        RERANK_LATENCY_SECONDS.observe(max(0.0, duration_s))
        if not ok:
            RERANK_ERRORS_TOTAL.inc()
    except Exception as exc:  # noqa: BLE001 - 观测降级：跳过记账，主链结果不受影响
        logger.warning("rerank metrics observation skipped: %s", exc)


# 动态跳过阈值（RERANK_DYNAMIC_SKIP_ENABLED，默认关）。
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


async def _call_rerank_rows(endpoint: str, payload: dict) -> list[dict]:
    """单批上游调用，返回原始 rows（``index`` 为批内局部下标）。

    这是**唯一**的上游调用与响应校验实现；单批与多批、串行与并发都走这里。
    """
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
    return rows


def _fuse_scored(scored: list[tuple[float, int]], docs: list) -> list:
    """全局融合的唯一真源：relevance_score 降序，同分按候选原始下标升序。

    单一实现 ⇒ 批次边界不影响同分次序，切批与不切批结果一致。
    """
    if not scored:
        raise ValueError("rerank response contains no valid document indices")
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [docs[idx] for _, idx in scored]


def _rerank_endpoint() -> str:
    return (
        os.getenv(
            "RERANKER_BASE_URL", os.getenv("EMBEDDING_BASE_URL", "http://127.0.0.1:1234/v1")
        ).rstrip("/")
        + "/rerank"
    )


async def _rerank_scored(
    query: str,
    docs: list,
    *,
    model: str | None,
    batch_size: int,
    max_concurrency: int,
    timeout: float,
) -> list:
    """切批 → 有界并发打上游 → 全局融合；任一失败整体抛出，由外层 fail-open。

    ``batch_size<=0`` 时单批覆盖全部候选（等价于旧的一次调用）；
    ``max_concurrency`` 只约束同时在飞的上游请求数，不改变融合口径。
    单批请求与整次重排共用同一个 ``timeout``。
    """
    endpoint = _rerank_endpoint()
    model_name = model or os.getenv("RERANKER_MODEL", _DEFAULT_RERANK_MODEL)
    size = len(docs) if batch_size <= 0 else max(1, int(batch_size))
    cap = max(0.1, float(timeout))
    semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))
    # 失败即熔断本轮：任一失败置位后，尚未开始打上游的批次直接空手返回，
    # 不再对着已知失败的上游继续堆请求（并发上限 >1 时只会有已在飞的几个）。
    aborted = False

    async def _batch(start: int) -> list[tuple[float, int]]:
        nonlocal aborted
        subset = docs[start : start + size]
        payload = {
            "model": model_name,
            "query": query,
            "documents": [getattr(doc, "page_content", "") for doc in subset],
            "top_n": len(subset),
        }
        async with semaphore:
            if aborted:
                return []
            begin = time.perf_counter()
            try:
                rows = await asyncio.wait_for(_call_rerank_rows(endpoint, payload), timeout=cap)
                # 行值解析必须在同一 except 之下：解析失败与请求失败对
                # rerank_errors_total 是同一种失败，不能一个记一个不记。
                batch_scored: list[tuple[float, int]] = []
                for row in rows:
                    local = int(row["index"])
                    if 0 <= local < len(subset):
                        batch_scored.append((float(row.get("relevance_score", 0)), start + local))
            except _RERANK_FAILURES:
                aborted = True
                _observe_rerank_call(time.perf_counter() - begin, ok=False)
                raise
            _observe_rerank_call(time.perf_counter() - begin, ok=True)
            return batch_scored

    results = await asyncio.gather(
        *[_batch(start) for start in range(0, len(docs), size)],
        return_exceptions=True,
    )
    failures = [r for r in results if isinstance(r, BaseException)]
    if failures:
        first = failures[0]
        # 取消（整次墙钟超时/上层放弃）必须原样上抛，不能被包成「批次失败」，
        # 否则 asyncio 的取消语义会被吞掉。
        if isinstance(first, asyncio.CancelledError):
            raise first
        raise ValueError(f"rerank batch failed: {first!r}")
    scored: list[tuple[float, int]] = []
    for batch in results:
        scored.extend(batch)
    return _fuse_scored(scored, docs)


async def _rerank_with_breaker(
    query: str,
    docs: list,
    *,
    model: str | None,
    batch_size: int,
    max_concurrency: int,
    timeout: float,
) -> tuple[list, bool]:
    """熔断 + 整体墙钟 + fail-open 的唯一实现，两个对外入口共用。"""
    if len(docs) < 2:
        return docs, False
    cap = max(0.1, float(timeout))
    try:
        from agentix.reliability.circuit_breaker import (
            CircuitBreakerHalfOpenError,
            CircuitBreakerOpenError,
            registry as breaker_registry,
        )
        from agentix.retrieval.settings import settings as _rs

        breaker = breaker_registry.get_or_create(
            _RERANK_BREAKER_NAME,
            # 直接读声明字段：RERANK_BREAKER_* 已在 retrieval/settings.py 正式声明，
            # 默认 5 / 60，不再用 getattr 字面量兜底（那样环境变量永远读不到）。
            failure_threshold=max(1, int(_rs.RERANK_BREAKER_FAILURE_THRESHOLD)),
            recovery_timeout=float(_rs.RERANK_BREAKER_RECOVERY_SECONDS),
        )
        # 整体包在一次 breaker.call 里：熔断计数保持「一次检索尝试失败」的语义，
        # 不按批计入失败——单批失败不该比整链失败更快触发 OPEN。
        ranked = await asyncio.wait_for(
            breaker.call(
                lambda: _rerank_scored(
                    query,
                    docs,
                    model=model,
                    batch_size=batch_size,
                    max_concurrency=max_concurrency,
                    timeout=cap,
                )
            ),
            timeout=cap,
        )
        return ranked, True
    except (CircuitBreakerOpenError, CircuitBreakerHalfOpenError):
        # 熔断打开/探测满：快速降级（原序返回），不发上游请求
        try:
            from agentix.observability.metrics import record_rerank_breaker

            record_rerank_breaker()
        except Exception:  # noqa: BLE001 - 观测降级绝不影响主链
            pass
        logger.info("rerank breaker open; serving original order")
        return docs, False
    except _RERANK_FAILURES as exc:
        logger.warning("rerank degraded (fail-open): %s", exc)
        return docs, False


async def rerank_documents_with_status(
    query: str, docs: list, model: str | None = None
) -> tuple[list, bool]:
    """返回重排结果及是否实际成功；不可用时按原序 fail-open。

    批次大小取 ``RETRIEVAL_RERANK_BATCH_SIZE``（默认 0 = 单批全候选），
    并发上限固定 1（该通道的既有形态：不并发打上游）。
    """
    return await _rerank_with_breaker(
        query,
        docs,
        model=model,
        batch_size=int(settings.RETRIEVAL_RERANK_BATCH_SIZE),
        max_concurrency=1,
        timeout=float(settings.RERANKER_TIMEOUT_SECONDS),
    )


async def rerank_documents(query: str, docs: list, model: str | None = None) -> list:
    """调用兼容 /rerank 的服务；服务不可用时保留原召回顺序。"""
    ranked, _ = await rerank_documents_with_status(query, docs, model)
    return ranked


async def rerank_documents_concurrent(
    query: str,
    docs: list,
    *,
    batch_size: int,
    max_concurrency: int,
    timeout: float,
    model: str | None = None,
) -> tuple[list, bool]:
    """Dense 候选池精排：连续分批 + 有界并发 + 整体超时，全局按分数融合。

    与 ``rerank_documents_with_status`` 是同一条实现，只是批次 / 并发 / 超时
    由调用方显式给出（Dense 通道读 ``RETRIEVAL_DENSE_RERANK_*``）。
    任一失败 / 超时 / 熔断打开 ⇒ ``(docs, False)``，由调用方 fail-open 保序。
    """
    return await _rerank_with_breaker(
        query,
        docs,
        model=model,
        batch_size=batch_size,
        max_concurrency=max_concurrency,
        timeout=timeout,
    )
