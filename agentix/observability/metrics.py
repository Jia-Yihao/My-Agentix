"""
可观测性 - Prometheus 指标收集

提供系统级和业务级指标，用于监控和告警。

使用方式：
    from agentix.observability.metrics import (
        REQUEST_COUNT, REQUEST_LATENCY, LLM_TOKEN_USAGE,
        record_request, record_llm_usage
    )
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from functools import wraps
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# 尝试导入 prometheus_client，不可用时降级
try:
    from prometheus_client import Counter, Histogram, Gauge, Info, generate_latest, CONTENT_TYPE_LATEST
    
    # ============================================================
    # HTTP 请求指标
    # ============================================================
    REQUEST_COUNT = Counter(
        "http_requests_total",
        "Total HTTP requests",
        ["method", "endpoint", "status"],
    )
    
    REQUEST_LATENCY = Histogram(
        "http_request_duration_seconds",
        "HTTP request latency in seconds",
        ["method", "endpoint"],
        buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    )
    
    REQUEST_SIZE = Histogram(
        "http_request_size_bytes",
        "HTTP request size in bytes",
        ["method", "endpoint"],
        buckets=[100, 1000, 10000, 100000, 1000000],
    )
    
    RESPONSE_SIZE = Histogram(
        "http_response_size_bytes",
        "HTTP response size in bytes",
        ["method", "endpoint"],
        buckets=[100, 1000, 10000, 100000, 1000000],
    )
    
    # ============================================================
    # RAG 管道指标
    # ============================================================
    RETRIEVAL_COUNT = Counter(
        "rag_retrieval_total",
        "Total RAG retrieval operations",
        ["kb_id", "engine", "status"],
    )
    
    RETRIEVAL_LATENCY = Histogram(
        "rag_retrieval_duration_seconds",
        "RAG retrieval latency",
        ["kb_id", "engine"],
        buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0],
    )
    
    RETRIEVAL_DOCS_COUNT = Histogram(
        "rag_retrieval_docs_count",
        "Number of documents retrieved per query",
        ["kb_id"],
        buckets=[1, 5, 10, 20, 50, 100],
    )
    
    RETRIEVAL_SCORE = Histogram(
        "rag_retrieval_score",
        "Retrieval relevance scores",
        ["kb_id", "method"],
        buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
    )
    
    # ============================================================
    # LLM 指标
    # ============================================================
    LLM_REQUEST_COUNT = Counter(
        "llm_requests_total",
        "Total LLM API requests",
        ["model", "provider", "status"],
    )
    
    LLM_REQUEST_LATENCY = Histogram(
        "llm_request_duration_seconds",
        "LLM API request latency",
        ["model", "provider"],
        buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0],
    )
    
    LLM_TOKEN_USAGE = Counter(
        "llm_tokens_total",
        "Total LLM tokens consumed",
        ["model", "type"],  # label values: prompt | completion
    )

    INDEX_JOB_DURATION = Histogram(
        "index_job_duration_seconds",
        "Index job duration",
        ["status"],
    )
    INDEX_JOB_FAILURES = Counter(
        "index_job_failures_total",
        "Index job failures",
        ["error_code"],
    )

    PURGE_VERIFY_FAILED = Counter(
        "retrieval_purge_verify_failed_total",
        "Document purge post-verification failures",
        ["kb_id"],
    )

    OUTPUT_FILTER_BLOCKED = Counter(
        "retrieval_output_filter_blocked_total",
        "Answer output filter blocks (harm / hallucination observe hits)",
        ["reason"],
    )

    APPROVAL_CREATED = Counter(
        "retrieval_approval_created_total",
        "Delete approval requests created",
        ["target_type"],
    )
    APPROVAL_DECISIONS = Counter(
        "retrieval_approval_decisions_total",
        "Delete approval decisions (approved / rejected)",
        ["target_type", "decision"],
    )
    APPROVAL_TIMEOUTS = Counter(
        "retrieval_approval_timeouts_total",
        "Delete approval requests processed after timeout",
        ["target_type"],
    )
    
    LLM_COST = Counter(
        "llm_cost_total",
        "Total LLM API cost in USD",
        ["model", "provider"],
    )

    RATE_LIMIT_TRIGGERED = Counter(
        "rate_limit_triggered_total",
        "Requests rejected by the global rate limit middleware",
        ["scope"],  # scope: global | chat（检索域聊天路由）
    )

    # 企业化 P1-2.1：降级事件计数（运维可感知"系统正处于降级态"）
    DEGRADED_TOTAL = Counter(
        "degraded_total",
        "Component degradation events (checkpointer downgrade, degraded chat responses...)",
        ["component"],
    )

    # P2 可观测（O1）：业务质量指标——答案证据锚定低分
    GROUNDING_LOW_SCORE = Counter(
        "grounding_low_score_total",
        "Answers below GROUNDING_LOW_SCORE_WARN grounding score (observe-only, delivery not blocked)",
    )

    # R-P2-2 证据门禁：warn/refuse 动作计数（observe 交付不计，动作可归因）
    EVIDENCE_GATE = Counter(
        "evidence_gate_total",
        "R-P2-2 evidence gate actions (warn=footer appended, refuse=answer withheld)",
        ["action"],
    )

    # P2 可观测（O1）：Model Gateway fallback 触发（主模型失败后链内兜底成功）
    GATEWAY_FALLBACK = Counter(
        "gateway_fallback_total",
        "Model Gateway fallback activations (primary failed, a fallback model answered)",
        ["primary_model", "fallback_model"],
    )

    # P2-1 满意度反馈：用户点赞/点踩计数（label 有界 up|down；
    # `rating` 标签名已加入 p100 ALLOWED_METRIC_LABELS 白名单）
    FEEDBACK_TOTAL = Counter(
        "feedback_total",
        "User message feedback counts (rating=up|down)",
        ["rating"],
    )

    # Stage F2c·A-P2-2：Multi-Agent 六失败模式计数（检测保留并打点，
    # NONE 不计——成功收敛不进本计数器，指标只反映失败归因）
    MULTIAGENT_FAILURE_MODE = Counter(
        "multiagent_failure_mode_total",
        "Multi-Agent manager failure modes detected (deadlock/livelock/echo_chamber/...)",
        ["mode"],
    )

    # 企业化 P1-2.2：熔断器状态与开合事件
    CIRCUIT_STATE = Gauge(
        "circuit_state",
        "Circuit breaker state (0=closed 1=half_open 2=open)",
        ["name"],
    )

    CIRCUIT_STATE_CHANGES = Counter(
        "circuit_state_changes_total",
        "Circuit breaker state transitions",
        ["name", "from_state", "to_state"],
    )

    # 企业化 P1-5.3：LLM 全局并发
    LLM_CONCURRENCY = Gauge(
        "llm_concurrency",
        "Current in-flight LLM calls (global semaphore occupancy)",
    )

    LLM_CONCURRENCY_REJECTED = Counter(
        "llm_concurrency_rejected_total",
        "LLM calls rejected because the global concurrency queue timed out",
    )

    # 企业化 P1-4.2：prompt injection 检测命中（stage: ingest | retrieval | output）
    PROMPT_INJECTION_DETECTED = Counter(
        "prompt_injection_detected_total",
        "Prompt injection pattern detections by pipeline stage",
        ["stage"],
    )
    
    # ============================================================
    # Agent 指标
    # ============================================================
    AGENT_STEP_COUNT = Counter(
        "agent_steps_total",
        "Total agent execution steps",
        ["agent_type", "step_type"],
    )
    
    AGENT_TOOL_CALLS = Counter(
        "agent_tool_calls_total",
        "Total agent tool invocations",
        ["tool_name", "status"],
    )
    
    AGENT_TOOL_LATENCY = Histogram(
        "agent_tool_duration_seconds",
        "Agent tool execution latency",
        ["tool_name"],
        buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0],
    )
    
    # ============================================================
    # 缓存指标
    # ============================================================
    CACHE_HITS = Counter(
        "cache_hits_total",
        "Total cache hits",
        ["cache_type"],
    )
    
    CACHE_MISSES = Counter(
        "cache_misses_total",
        "Total cache misses",
        ["cache_type"],
    )
    
    CACHE_SIZE = Gauge(
        "cache_size",
        "Current cache size",
        ["cache_type"],
    )
    
    # ============================================================
    # 系统指标
    # ============================================================
    ACTIVE_CONNECTIONS = Gauge(
        "active_connections",
        "Number of active connections",
        ["type"],
    )
    
    SYSTEM_INFO = Info(
        "system",
        "System information",
    )
    
    # ============================================================
    # 业务指标
    # ============================================================
    CHAT_CONVERSATIONS = Counter(
        "chat_conversations_total",
        "Total chat conversations created",
        ["kb_id"],
    )
    
    CHAT_MESSAGES = Counter(
        "chat_messages_total",
        "Total chat messages",
        ["kb_id", "role"],
    )
    
    CITATION_COUNT = Histogram(
        "chat_citations_count",
        "Number of citations per answer",
        ["kb_id"],
        buckets=[0, 1, 2, 3, 5, 10],
    )
    
    # ============================================================
    # Phase H · W0 性能真值指标
    # ============================================================
    TTFT_LATENCY = Histogram(
        "agent_ttft_seconds",
        "Stream start to first content frame (SSE TTFT)",
        buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
    )
    
    AGENT_LLM_CALLS = Counter(
        "agent_llm_call_total",
        "LLM calls by agent role (bounded enum)",
        ["role", "status"],  # role: router/planner/tool/summarizer/general/hallucination/retrieval
    )
    
    AGENT_LLM_TOKENS = Counter(
        "agent_llm_tokens_total",
        "LLM tokens by agent role",
        ["role", "type"],  # 标签 type 取值 prompt / completion（账本方向）
    )
    
    EVENT_LOOP_LAG = Histogram(
        "event_loop_lag_seconds",
        "Asyncio event loop scheduling lag (sampling task)",
        buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0],
    )

    # ============================================================
    # Phase H · W1 LLM 优化指标
    # ============================================================
    LLM_CALLS_SAVED = Counter(
        "llm_calls_saved_total",
        "LLM calls avoided by deterministic routing fast-path",
        ["reason"],  # reason: chit_chat / statute_direct（有界枚举，控基数）
    )

    PROMPT_PREFIX_FINGERPRINT = Gauge(
        "prompt_prefix_fingerprint",
        "CRC32 fingerprint of the static system-prompt prefix per builder (prompt drift detection)",
        ["builder"],
    )

    # ============================================================
    # Phase H · W3 运行时治理指标
    # ============================================================
    ADMISSION_DECISIONS = Counter(
        "admission_decisions_total",
        "Run admission three-state decisions (admitted/queued/rejected)",
        ["state"],  # state: admitted / queued / rejected（有界枚举）
    )

    # ============================================================
    # Phase H · W4 持久化与 SSE 出口指标
    # ============================================================
    SSE_DROPPED_FRAMES = Counter(
        "sse_dropped_frames_total",
        "SSE egress frames dropped by bounded-queue backpressure (terminal frames are never dropped)",
        ["reason"],  # reason: backpressure（有界枚举）
    )

    # ============================================================
    # LG-3-3 重试与恢复的业务指标
    # ============================================================
    # 两个 label 的取值都是封闭枚举（``RETRY_LAYER_VALUES`` /
    # ``GRAPH_RESUME_KIND_VALUES``，见下方 record_* 区）；越界值一律落 ``"other"``，
    # 指标基数由枚举封死——thread_id / user_id 这类高基数标识只进 span。
    RETRY_ATTEMPTS = Counter(
        "retry_attempts_total",
        "Retry attempts actually started per retry layer (first attempt not counted)",
        ["layer"],  # layer: tool / task / retrieval（有界枚举）
    )

    RETRY_SLEEP_SECONDS = Counter(
        "retry_sleep_seconds_total",
        "Backoff seconds actually slept per retry layer",
        ["layer"],  # layer: tool / task / retrieval（有界枚举）
    )

    RETRY_EXHAUSTED = Counter(
        "retry_exhausted_total",
        "Retry chains that failed without a further attempt (attempts used up or budget denied)",
        ["layer"],  # layer: tool / task / retrieval（有界枚举）
    )

    GRAPH_RESUME = Counter(
        "graph_resume_total",
        "Graph resumes from checkpoint by entry point",
        ["kind"],  # kind: startup / ops / retry_task / hitl_in_loop（有界枚举）
    )

    PROMETHEUS_AVAILABLE = True
    logger.info("Prometheus metrics enabled")

except ImportError:
    PROMETHEUS_AVAILABLE = False
    logger.warning("prometheus_client not installed, metrics disabled")
    
    # 降级：空操作
    class _NoopMetric:
        def labels(self, *args, **kwargs): return self
        def inc(self, amount=1): pass
        def dec(self, amount=1): pass
        def set(self, value): pass
        def observe(self, value): pass
        def info(self, value): pass
    
    REQUEST_COUNT = _NoopMetric()
    REQUEST_LATENCY = _NoopMetric()
    REQUEST_SIZE = _NoopMetric()
    RESPONSE_SIZE = _NoopMetric()
    RETRIEVAL_COUNT = _NoopMetric()
    RETRIEVAL_LATENCY = _NoopMetric()
    RETRIEVAL_DOCS_COUNT = _NoopMetric()
    RETRIEVAL_SCORE = _NoopMetric()
    LLM_REQUEST_COUNT = _NoopMetric()
    LLM_REQUEST_LATENCY = _NoopMetric()
    LLM_TOKEN_USAGE = _NoopMetric()
    INDEX_JOB_DURATION = _NoopMetric()
    INDEX_JOB_FAILURES = _NoopMetric()
    PURGE_VERIFY_FAILED = _NoopMetric()
    OUTPUT_FILTER_BLOCKED = _NoopMetric()
    APPROVAL_CREATED = _NoopMetric()
    APPROVAL_DECISIONS = _NoopMetric()
    APPROVAL_TIMEOUTS = _NoopMetric()
    LLM_COST = _NoopMetric()
    RATE_LIMIT_TRIGGERED = _NoopMetric()
    DEGRADED_TOTAL = _NoopMetric()
    GROUNDING_LOW_SCORE = _NoopMetric()
    EVIDENCE_GATE = _NoopMetric()
    GATEWAY_FALLBACK = _NoopMetric()
    MULTIAGENT_FAILURE_MODE = _NoopMetric()
    CIRCUIT_STATE = _NoopMetric()
    CIRCUIT_STATE_CHANGES = _NoopMetric()
    LLM_CONCURRENCY = _NoopMetric()
    LLM_CONCURRENCY_REJECTED = _NoopMetric()
    PROMPT_INJECTION_DETECTED = _NoopMetric()
    AGENT_STEP_COUNT = _NoopMetric()
    AGENT_TOOL_CALLS = _NoopMetric()
    AGENT_TOOL_LATENCY = _NoopMetric()
    CACHE_HITS = _NoopMetric()
    CACHE_MISSES = _NoopMetric()
    CACHE_SIZE = _NoopMetric()
    ACTIVE_CONNECTIONS = _NoopMetric()
    SYSTEM_INFO = _NoopMetric()
    CHAT_CONVERSATIONS = _NoopMetric()
    CHAT_MESSAGES = _NoopMetric()
    CITATION_COUNT = _NoopMetric()
    TTFT_LATENCY = _NoopMetric()
    AGENT_LLM_CALLS = _NoopMetric()
    AGENT_LLM_TOKENS = _NoopMetric()
    EVENT_LOOP_LAG = _NoopMetric()
    LLM_CALLS_SAVED = _NoopMetric()
    PROMPT_PREFIX_FINGERPRINT = _NoopMetric()
    ADMISSION_DECISIONS = _NoopMetric()
    SSE_DROPPED_FRAMES = _NoopMetric()
    RETRY_ATTEMPTS = _NoopMetric()
    RETRY_SLEEP_SECONDS = _NoopMetric()
    RETRY_EXHAUSTED = _NoopMetric()
    GRAPH_RESUME = _NoopMetric()


# ============================================================
# 便捷函数
# ============================================================

def record_request(method: str, endpoint: str, status: int, duration: float):
    """记录 HTTP 请求指标"""
    REQUEST_COUNT.labels(method=method, endpoint=endpoint, status=str(status)).inc()
    REQUEST_LATENCY.labels(method=method, endpoint=endpoint).observe(duration)


def record_retrieval(kb_id: str, engine: str, status: str, duration: float, docs_count: int):
    """记录检索指标"""
    RETRIEVAL_COUNT.labels(kb_id=kb_id, engine=engine, status=status).inc()
    RETRIEVAL_LATENCY.labels(kb_id=kb_id, engine=engine).observe(duration)
    RETRIEVAL_DOCS_COUNT.labels(kb_id=kb_id).observe(docs_count)


def record_llm_usage(model: str, provider: str, prompt_tokens: int, completion_tokens: int, duration: float, cost: float = 0.0):
    """记录 LLM 使用指标"""
    LLM_REQUEST_COUNT.labels(model=model, provider=provider, status="success").inc()
    LLM_REQUEST_LATENCY.labels(model=model, provider=provider).observe(duration)
    LLM_TOKEN_USAGE.labels(model=model, type="prompt").inc(prompt_tokens)
    LLM_TOKEN_USAGE.labels(model=model, type="completion").inc(completion_tokens)
    if cost > 0:
        LLM_COST.labels(model=model, provider=provider).inc(cost)


def record_llm_tokens(
    model: str, direction: str, value: int, label_type: Optional[str] = None
) -> None:
    """M6.3: 记录 LLM token 用量指标（direction=in/out 映射到 prompt/completion）。

    ``label_type`` 显式覆盖 type 标签取值（如 ``cached`` = prompt 缓存命中量，
    上下文工程 A2）；缺省仍按 direction 推导，既有调用点行为不变。
    """
    if value <= 0:
        return
    resolved = label_type or ("prompt" if direction == "in" else "completion")
    LLM_TOKEN_USAGE.labels(model=model, type=resolved).inc(value)


def record_ttft(duration: float) -> None:
    """Phase H·W0：SSE 流首内容帧时延（TTFT），单位秒。"""
    TTFT_LATENCY.observe(max(0.0, duration))


def record_agent_llm_call(role: str, status: str) -> None:
    """Phase H·W0：按角色统计 LLM 调用数（role 为有界枚举，控基数）。"""
    AGENT_LLM_CALLS.labels(role=role or "unknown", status=status or "unknown").inc()


def record_agent_llm_tokens(role: str, prompt_tokens: int, completion_tokens: int) -> None:
    """Phase H·W0：按角色统计 LLM token 用量（prompt/completion 两个方向）。"""
    label = role or "unknown"
    if prompt_tokens > 0:
        AGENT_LLM_TOKENS.labels(role=label, type="prompt").inc(prompt_tokens)
    if completion_tokens > 0:
        AGENT_LLM_TOKENS.labels(role=label, type="completion").inc(completion_tokens)


#: Phase H · W3：最近一次采样的 event-loop lag（负载阶梯输入；flag 关时
#: 采样任务不启动、此值保持 0）。仅事件循环内单线程写读，无锁。
_last_event_loop_lag: float = 0.0


def get_last_event_loop_lag() -> float:
    """Phase H · W3：读最近一次 lag 采样（秒；未采样 = 0.0）。"""
    return _last_event_loop_lag


def record_event_loop_lag(lag: float) -> None:
    """Phase H · W0：事件循环调度滞后（后台采样任务上报，flag 默认关）。"""
    global _last_event_loop_lag
    _last_event_loop_lag = max(0.0, lag)
    EVENT_LOOP_LAG.observe(_last_event_loop_lag)


def record_admission_decision(state: str) -> None:
    """Phase H · W3：准入三态计数（state: admitted/queued/rejected）。"""
    ADMISSION_DECISIONS.labels(state=state or "unknown").inc()


def record_sse_dropped(reason: str = "backpressure") -> None:
    """Phase H · W4：SSE 有界队列背压丢帧计数（终态帧永不丢）。"""
    SSE_DROPPED_FRAMES.labels(reason=reason or "backpressure").inc()


#: LG-3-3：重试层名的封闭枚举——与 ``RetryBudget.try_consume(layer=...)``
#: 的实参取值同源（tool / task / retrieval 三层）。枚举外的值落 ``"other"``。
RETRY_LAYER_VALUES = frozenset({"tool", "task", "retrieval"})

#: LG-3-3：图续跑入口的封闭枚举——四个生产入口各占一个值
#: （启动恢复 / ops 手工续跑 / ops 定点重跑 / 进环 HITL 裁决恢复）。
GRAPH_RESUME_KIND_VALUES = frozenset({"startup", "ops", "retry_task", "hitl_in_loop"})


def _bounded_label(value: str, allowed: frozenset[str]) -> str:
    """把 label 取值收进封闭枚举：越界落 ``"other"``，基数不随调用方自由串膨胀。"""
    return value if value in allowed else "other"


def record_retry_attempt(layer: str) -> None:
    """LG-3-3：某一层实际发起的**重试**次数（首次尝试不计）。"""
    RETRY_ATTEMPTS.labels(layer=_bounded_label(layer, RETRY_LAYER_VALUES)).inc()


def record_retry_sleep(layer: str, seconds: float) -> None:
    """LG-3-3：某一层实际退避停等的秒数（被预算截断、没真停的不计）。"""
    if seconds <= 0:
        return
    RETRY_SLEEP_SECONDS.labels(layer=_bounded_label(layer, RETRY_LAYER_VALUES)).inc(seconds)


def record_retry_exhausted(layer: str) -> None:
    """LG-3-3：某一层以失败收场且不再有下一次尝试（次数用尽或被预算拒批）。"""
    RETRY_EXHAUSTED.labels(layer=_bounded_label(layer, RETRY_LAYER_VALUES)).inc()


def record_graph_resume(kind: str) -> None:
    """LG-3-3：向图发起一次续跑（计数点是**发起**，不是续跑成功）。"""
    GRAPH_RESUME.labels(kind=_bounded_label(kind, GRAPH_RESUME_KIND_VALUES)).inc()


def record_llm_calls_saved(reason: str) -> None:
    """Phase H·W1：确定性 fast-path 跳过的 LLM 意图分类调用数（reason 有界枚举）。"""
    LLM_CALLS_SAVED.labels(reason=reason or "unknown").inc()


def record_prompt_prefix_fingerprint(builder: str, prefix: str) -> None:
    """Phase H·W1：静态 system prompt 前缀指纹（CRC32 → Gauge）。

    前缀漂移在 Prometheus/Grafana 侧可见（同 builder 指纹突变 = 提示词
    静态段被改）；CRC32 < 2^32，float64 精确可表示，不丢精度。
    """
    import zlib

    PROMPT_PREFIX_FINGERPRINT.labels(builder=builder or "unknown").set(
        float(zlib.crc32(prefix.encode("utf-8")) & 0xFFFFFFFF)
    )


def record_index_job(duration: float, status: str, error_code: str = ""):
    INDEX_JOB_DURATION.labels(status=status).observe(max(0.0, duration))
    if status == "failed":
        INDEX_JOB_FAILURES.labels(error_code=error_code or "unknown").inc()


def record_purge_verify_failed(kb_id: str) -> None:
    """记录删除后置校验失败（chunk / manifest / 文档记录仍有残留）。"""
    PURGE_VERIFY_FAILED.labels(kb_id=kb_id).inc()


def record_output_filter_blocked(reason: str) -> None:
    """记录输出过滤命中（reason: harm / hallucination）。"""
    OUTPUT_FILTER_BLOCKED.labels(reason=reason or "unknown").inc()


def record_approval_created(target_type: str) -> None:
    """记录删除审批请求创建。"""
    APPROVAL_CREATED.labels(target_type=target_type or "unknown").inc()


def record_approval_decision(target_type: str, decision: str) -> None:
    """记录审批决策（decision: approved / rejected）。"""
    APPROVAL_DECISIONS.labels(
        target_type=target_type or "unknown", decision=decision or "unknown"
    ).inc()


def record_approval_timeout(target_type: str) -> None:
    """记录超时审批被处理。"""
    APPROVAL_TIMEOUTS.labels(target_type=target_type or "unknown").inc()


def record_cache_hit(cache_type: str):
    """记录缓存命中"""
    CACHE_HITS.labels(cache_type=cache_type).inc()


def record_rate_limit_triggered(scope: str = "global"):
    """记录限流触发（scope: global=全局限流中间件 / chat=检索域聊天路由）"""
    RATE_LIMIT_TRIGGERED.labels(scope=scope).inc()


def record_degraded(component: str) -> None:
    """企业化 P1-2.1：记录组件降级事件（component: checkpointer / chat / retrieval...）"""
    DEGRADED_TOTAL.labels(component=component or "unknown").inc()


def record_grounding_low_score() -> None:
    """P2 可观测（O1）：grounding 低分答案 +1（观测口径，不拦截交付）"""
    GROUNDING_LOW_SCORE.inc()


def record_evidence_gate(action: str) -> None:
    """R-P2-2 证据门禁：warn/refuse 动作 +1（observe 交付不计，动作可归因）"""
    EVIDENCE_GATE.labels(action=action or "unknown").inc()


def record_multiagent_failure(mode: str) -> None:
    """Stage F2c·A-P2-2：六失败模式检测打点（mode=FailureMode.value；NONE 不计）"""
    MULTIAGENT_FAILURE_MODE.labels(mode=mode or "unknown").inc()



def record_gateway_fallback(primary_model: str, fallback_model: str) -> None:
    """P2 可观测（O1）：Model Gateway 主模型失败后由 fallback 模型兜底成功一次"""
    GATEWAY_FALLBACK.labels(
        primary_model=primary_model or "unknown",
        fallback_model=fallback_model or "unknown",
    ).inc()


def record_feedback(rating: str) -> None:
    """P2-1 满意度反馈打点（rating ∈ up|down，越界值忽略不记账）"""
    if rating in ("up", "down"):
        FEEDBACK_TOTAL.labels(rating=rating).inc()


_CIRCUIT_STATE_VALUE = {"closed": 0, "half_open": 1, "open": 2}


def record_circuit_state(name: str, state: str) -> None:
    """企业化 P1-2.2：设置熔断器当前状态 Gauge（closed=0 half_open=1 open=2）"""
    CIRCUIT_STATE.labels(name=name).set(_CIRCUIT_STATE_VALUE.get(state, -1))


def record_circuit_transition(name: str, from_state: str, to_state: str) -> None:
    """企业化 P1-2.2：记录熔断器状态转换事件（含初始 closed 打点对齐 Gauge）"""
    CIRCUIT_STATE_CHANGES.labels(
        name=name, from_state=from_state or "none", to_state=to_state or "none"
    ).inc()
    record_circuit_state(name, to_state)


def record_llm_concurrency_occupied() -> None:
    """企业化 P1-5.3：LLM 全局并发 +1"""
    LLM_CONCURRENCY.inc()


def record_llm_concurrency_released() -> None:
    """企业化 P1-5.3：LLM 全局并发 -1"""
    LLM_CONCURRENCY.dec()


def record_llm_concurrency_rejected() -> None:
    """企业化 P1-5.3：并发排队超时被拒绝的 LLM 调用"""
    LLM_CONCURRENCY_REJECTED.inc()


def record_prompt_injection(stage: str) -> None:
    """企业化 P1-4.2：记录 prompt injection 模式命中（stage: ingest/retrieval/output）"""
    PROMPT_INJECTION_DETECTED.labels(stage=stage or "unknown").inc()


def record_cache_miss(cache_type: str):
    """记录缓存未命中"""
    CACHE_MISSES.labels(cache_type=cache_type).inc()


def record_tool_call(tool_name: str, status: str, duration: float):
    """记录工具调用"""
    AGENT_TOOL_CALLS.labels(tool_name=tool_name, status=status).inc()
    AGENT_TOOL_LATENCY.labels(tool_name=tool_name).observe(duration)


@asynccontextmanager
async def track_latency(metric, **labels):
    """异步上下文管理器：自动记录耗时"""
    start = time.perf_counter()
    try:
        yield
    finally:
        duration = time.perf_counter() - start
        metric.labels(**labels).observe(duration)


def timed(metric_name: str, **labels):
    """装饰器：自动记录函数耗时"""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                result = await func(*args, **kwargs)
                return result
            finally:
                duration = time.perf_counter() - start
                # 根据 metric_name 获取对应的 metric 对象
                metric_map = {
                    "request": REQUEST_LATENCY,
                    "retrieval": RETRIEVAL_LATENCY,
                    "llm": LLM_REQUEST_LATENCY,
                    "tool": AGENT_TOOL_LATENCY,
                }
                m = metric_map.get(metric_name)
                if m:
                    m.labels(**labels).observe(duration)
        return wrapper
    return decorator


def get_metrics() -> bytes:
    """获取 Prometheus 指标数据"""
    if PROMETHEUS_AVAILABLE:
        return generate_latest()
    return b""


def get_metrics_content_type() -> str:
    """获取 Prometheus 指标 Content-Type"""
    if PROMETHEUS_AVAILABLE:
        return CONTENT_TYPE_LATEST
    return "text/plain"
