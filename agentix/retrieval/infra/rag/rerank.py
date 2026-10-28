"""Agent 检索结果重排适配器。

R1-3（依据《技术变更与架构演进指南 v2.0》§2.1.5③ 外部依赖熔断）：
- 显式 timeout（既有）+ **熔断**（新增）：rerank 服务连续失败达阈值后
  进入 OPEN，后续请求快速跳过重排（保留原召回顺序），不再每请求空耗
  一次超时等待；恢复超时后半开探测，成功即回 CLOSED。
- 降级语义（开闸降级）：跳过重排 = 按原序返回，答案质量下降但可用，
  与指南"跳过重排、只回本地、标注来源受限"一致。
- T9（approval-recovery-optimization §T9）：``RETRIEVAL_RERANK_BATCH_SIZE``>0
  时候选分批打上游（降低单请求负载），默认 0 = 单次全候选调用（与基线一致）；
  并补检索侧计数器 rerank_calls_total / rerank_errors_total /
  rerank_latency_seconds（零 label，旁路观测 fail-open）。
"""

from __future__ import annotations

import os
import logging
import time

import httpx

from agentix.retrieval.settings import settings

logger = logging.getLogger(__name__)
_RERANK_BREAKER_NAME = "rerank_service"

# T9（approval-recovery-optimization §T9）：上游异常判定集合——"什么算一次失败的
# 上游调用"的唯一真源（纪律 6 ④：同一个判定不许两个源）：分批/单次两条路径的
# 失败记账与 rerank_documents_with_status 外层 fail-open 捕获的都**直接是本常量**，
# 不得再另写字面量元组。
_RERANK_UPSTREAM_ERRORS = (httpx.HTTPError, KeyError, TypeError, ValueError)

# T9 检索侧计数器（验收②/纪律 3）。零 label ⇒ 基数恒为 1，天然不受
# tests/test_p100_genai_observability.py 的 ALLOWED_METRIC_LABELS 守卫影响——
# 该守卫实测枚举的是默认 REGISTRY 全部 collector 的 label 名（p100:284-305），
# 无 label 的指标不在它的对照面上，故不为其新增白名单项。
# 落点选在本模块而非 agentix/observability/metrics.py：后者正被另一并行批次
# 独占（硬边界禁改）；prometheus_client 的导出真源是默认 REGISTRY
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
    """T9 记账一次上游调用（耗时 + 成败）。

    观测出错绝不影响主链检索（纪律 2）：捕获后 warning 留痕并跳过——
    指标可以缺，rerank 结果不能炸。
    """
    try:
        RERANK_CALLS_TOTAL.inc()
        RERANK_LATENCY_SECONDS.observe(max(0.0, duration_s))
        if not ok:
            RERANK_ERRORS_TOTAL.inc()
    except Exception as exc:  # noqa: BLE001 - 观测降级：跳过记账，主链结果不受影响
        logger.warning("rerank metrics observation skipped: %s", exc)


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
    # 分批路径 _call_rerank_batch_rows 刻意逐字复刻本函数的响应校验段：改一处必须同步另一处（不共用重构的理由见该函数 docstring）。
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


async def _call_rerank_batch_rows(endpoint: str, payload: dict) -> list[dict]:
    """单批上游调用，返回原始 rows（``index`` 为批内局部下标）。

    响应校验刻意**复刻** ``_call_rerank`` 的口径而不是重构共用：``_call_rerank``
    是 flag-off 的基线路径，纪律 1 要求它的调用形状零改动（reviewer 最硬核对项
    是逐位等价），共用重构会把分批的变更面掺进基线，反而更难核对。
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


async def _rerank_batched(endpoint: str, base_payload: dict, docs: list, batch_size: int) -> list:
    """T9 候选分批：全候选按 batch_size 切连续批次，逐批打上游，再全局融合。

    融合口径（验收③的显式声明）：所有批次的行按上游 ``relevance_score``
    **降序**排；**同分按候选原始下标升序**——确定性、可复现，批次边界不影响
    同分次序。与不分批的差别仅此一条（单次调用同分次序由上游 rows 返回顺序
    经稳定排序决定），分数可全量比较时分批与不分批结果一致。

    任一批次失败整体抛出 ⇒ 熔断按"一次检索尝试"记一次失败，上层 fail-open
    保原序——与单次调用失败的降级语义完全一致，分批不新增失败面。
    """
    scored: list[tuple[float, int]] = []  # (relevance_score, 全局候选下标)
    total = len(docs)
    for start in range(0, total, batch_size):
        subset = docs[start : start + batch_size]
        payload = {
            "model": base_payload["model"],
            "query": base_payload["query"],
            "documents": [getattr(doc, "page_content", "") for doc in subset],
            "top_n": len(subset),
        }
        begin = time.perf_counter()
        try:
            rows = await _call_rerank_batch_rows(endpoint, payload)
            # 行值解析必须计入与单次同形的失败记账范围：单次路径的 int/float 解析
            # 发生在 _call_rerank 内、同一 except 之下（解析失败记 ok=False），分批
            # 若把解析挪到 ok=True 之后，同一件事两条路径 errors_total 口径相反。
            batch_scored: list[tuple[float, int]] = []
            for row in rows:
                local = int(row["index"])
                if 0 <= local < len(subset):
                    batch_scored.append((float(row.get("relevance_score", 0)), start + local))
        except _RERANK_UPSTREAM_ERRORS:
            _observe_rerank_call(time.perf_counter() - begin, ok=False)
            raise
        _observe_rerank_call(time.perf_counter() - begin, ok=True)
        scored.extend(batch_scored)
    if not scored:
        raise ValueError("rerank response contains no valid document indices")
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [docs[idx] for _, idx in scored]


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
            # T9：RETRIEVAL_RERANK_BATCH_SIZE<=0（默认）⇒ 单次全候选调用，基线
            # 路径逐位不变；只在外围加计数器（验收②要求不分批也可观测，计数器
            # 是调用方不可见的旁路观测，不属行为等价面）。>0 ⇒ 分批路径。
            batch_size = int(settings.RETRIEVAL_RERANK_BATCH_SIZE)
            if batch_size <= 0:
                begin = time.perf_counter()
                try:
                    ranked = await _call_rerank(endpoint, payload, docs)
                except _RERANK_UPSTREAM_ERRORS:
                    _observe_rerank_call(time.perf_counter() - begin, ok=False)
                    raise
                _observe_rerank_call(time.perf_counter() - begin, ok=True)
                return ranked
            # 分批整体包在一次 breaker.call 里：熔断计数保持"一次检索尝试失败"
            # 的语义（与单次调用一致），不按批计入失败——单批失败不该比整链失败
            # 更快触发 OPEN。
            return await _rerank_batched(endpoint, payload, docs, batch_size)

        return await breaker.call(_call), True
    except (CircuitBreakerOpenError, CircuitBreakerHalfOpenError):
        # 熔断打开/探测满：快速降级（原序返回），不发上游请求
        logger.info("rerank breaker open; serving original order")
        return docs, False
    except _RERANK_UPSTREAM_ERRORS as exc:
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
