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
from contextvars import ContextVar
from functools import wraps
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class _GatewayFallbackFact:
    """T6：本流「Model Gateway 兜底」事实槽（单个可变对象）。

    写入方只有一个：回退链自身的成败判定点（model_gateway.ainvoke_with_fallback
    的既有 `if errors:` 分支）经 record_gateway_fallback 置位——「是否走了兜底」
    在仓里不存在第二个判定（纪律 6 ④）。读取方：RunEventEmitter 首内容帧记 TTFT
    时判归因。

    用「槽对象」而非直接 set 布尔值：并发工具轮（AGENT_TOOL_ROUND_CONCURRENCY>1）
    里工具跑在 gather 子任务——ContextVar.set 不回传父任务，但子任务创建时复制的
    绑定指向**同一个槽引用**，改属性父侧可见。
    """

    __slots__ = ("observed",)

    def __init__(self) -> None:
        self.observed = False


# 刻意定义在 prometheus try-import 之外：无指标后端时归因通道照常工作（noop 降级）。
_gateway_fallback_fact: ContextVar[Optional[_GatewayFallbackFact]] = ContextVar(
    "gateway_fallback_fact", default=None
)


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
        "Approval decisions (decision: approved/rejected/edited/resumed/auto/"
        "bypass; target_type: tool/document/kb, empty falls back to"
        " unknown)",
        ["target_type", "decision"],
    )
    # 覆盖两类到期裁决（删除审批 / 工具审批时限）：help 只写其中一类会让运维
    # 误判指标来源，而指标名与 label 不变（口径收敛不加新序列）。
    APPROVAL_TIMEOUTS = Counter(
        "retrieval_approval_timeouts_total",
        "Approval requests decided only after their waiting deadline elapsed"
        " (timeout attribution, decision counted separately)",
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

    # T22：checkpointer 降级后的升级重试结果（`result` 封闭 ok|fail，有界；已在
    # p100 ALLOWED_METRIC_LABELS 白名单）。与 `degraded_total` 分工：那条记"进了降级态"，
    # 这条记"有没有从降级态回来"——只有它能在降级后告诉运维"多久没回来"。
    CHECKPOINTER_UPGRADE = Counter(
        "checkpointer_upgrade_total",
        "Checkpointer upgrade retries after MemorySaver degradation (result=ok|fail)",
        ["result"],
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

    # R-P3：路由兜底触发（L1 分类失败/非法 ⇒ 落 kb-query 兜底或将判据降级为启发式）。
    # reason 有界 exception|empty|invalid_type|unknown；`reason` 标签名已在 p100 白名单。
    # 补的是 §16 记录的那处空洞：这条路径原先只有一行 warning，`degraded_total`
    # 与 `gateway_fallback_total` 都不覆盖它。
    ROUTER_FALLBACK = Counter(
        "router_fallback_total",
        "Router L1 classification fallbacks (reason=exception|empty|invalid_type|unknown)",
        ["reason"],
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

    # T6（approval-recovery-optimization）：拆开 TTFT 混采——走过 Model Gateway
    # 兜底链的流，其首内容帧样本进本指标，不再混进主口径。选「独立指标」而非
    # 「加 label」：主直方图定义与记录点逐字不动（README §15 PromQL、p92 断言、
    # 历史桶全保住），且零新增 label（纪律 3，p100 白名单无需扩容）。
    # 桶边界**逐字复制**主直方图：Q2 未决不细分，两口径同分辨率才可互比。
    TTFT_LATENCY_FALLBACK = Histogram(
        "agent_ttft_fallback_seconds",
        "Stream start to first content frame (SSE TTFT) - Model Gateway fallback path",
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
        # reason 的取值由 agentix/application/agents/router.py::FASTPATH_REASONS
        # 单源定义（封闭枚举，控基数）——本注释只做指针，不许在此再抄一份名单。
        ["reason"],
    )

    ROUTING_CACHE = Counter(
        "routing_cache_total",
        "Process-local routing result cache outcomes (hit / miss / error)",
        ["result"],  # result: hit / miss / error（封闭枚举，控基数）
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

    # T26：Redis Stream 任务队列的 PEL 认领轮次（`result` 封闭 ok|empty|error，有界；
    # 标签名 `result` 已在 p100 ALLOWED_METRIC_LABELS 白名单）。与消费计数分开的理由：
    # 「正常消费」和「接手死消费者遗留消息」是两条不同的健康信号——只有后者能回答
    # 「有没有 worker 死在 PEL 里、现在有人在接手吗」。
    TASK_QUEUE_CLAIMED = Counter(
        "task_queue_claimed_total",
        "Redis Stream PEL claim rounds via XAUTOCLAIM (result=ok|empty|error)",
        ["result"],
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
    TTFT_LATENCY_FALLBACK = _NoopMetric()
    AGENT_LLM_CALLS = _NoopMetric()
    AGENT_LLM_TOKENS = _NoopMetric()
    EVENT_LOOP_LAG = _NoopMetric()
    LLM_CALLS_SAVED = _NoopMetric()
    ROUTING_CACHE = _NoopMetric()
    PROMPT_PREFIX_FINGERPRINT = _NoopMetric()
    ADMISSION_DECISIONS = _NoopMetric()
    SSE_DROPPED_FRAMES = _NoopMetric()
    RETRY_ATTEMPTS = _NoopMetric()
    RETRY_SLEEP_SECONDS = _NoopMetric()
    RETRY_EXHAUSTED = _NoopMetric()
    GRAPH_RESUME = _NoopMetric()
    # T22：兜底表逐名赋值，漏一个 ⇒ 无 prometheus 后端时 record_* 抛 NameError
    # （被调用点的宽 except 吞掉，指标静默消失）。新增指标必须在这里同步一行。
    CHECKPOINTER_UPGRADE = _NoopMetric()
    TASK_QUEUE_CLAIMED = _NoopMetric()


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
    """Phase H·W0：SSE 流首内容帧时延（TTFT），单位秒。T6 后为**主口径**（未走兜底的流）。"""
    TTFT_LATENCY.observe(max(0.0, duration))


def record_ttft_fallback(duration: float) -> None:
    """T6：兜底路径的 TTFT（Model Gateway 主模型失败、由链内兜底模型答出），单位秒。

    本函数只在发射器读到 ``gateway_fallback_observed()`` 为真时被调用——判定由
    回退链自身导出（见 :class:`_GatewayFallbackFact`），这里不另立判据（纪律 6 ④）。
    """
    TTFT_LATENCY_FALLBACK.observe(max(0.0, duration))


def begin_gateway_fallback_fact() -> _GatewayFallbackFact:
    """T6：新流开始（RunEventEmitter 构造点）为本流开一个新的兜底事实槽。

    同上下文中上一流的残留事实随之清零——归因只属于**本流**的生命周期。
    """
    fact = _GatewayFallbackFact()
    _gateway_fallback_fact.set(fact)
    return fact


def gateway_fallback_observed() -> bool:
    """T6：当前流的回退链是否已导出「主模型失败、链内兜底成功」的事实。

    事实槽未开（构造失败/非流式上下文）时恒 False——宁可少一笔兜底归因，
    也不在仓里出现第二个「是否兜底」的判定点（纪律 6 ④）。
    """
    fact = _gateway_fallback_fact.get()
    return fact is not None and fact.observed


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


#: T26：PEL 认领轮次结果的封闭枚举。调用点只有
#: ``agentix/application/services/task_queue.py::TaskQueue._claim_stale_pending``，
#: 且只传这三个字面量——词表外取值不可达，故这里不另设 ``"other"`` 兜底
#: （在指标层改口会把"调用点传错了"这件事一起吞掉）。
TASK_QUEUE_CLAIM_RESULT_VALUES = frozenset({"ok", "empty", "error"})


def record_task_queue_claimed(result: str) -> None:
    """T26：记一轮 PEL 认领的结果（``ok``=认领到并已交给处理器 / ``empty``=没有
    到期消息 / ``error``=XAUTOCLAIM 失败，异常已由调用点 warning 留痕）。"""
    TASK_QUEUE_CLAIMED.labels(result=result).inc()


def record_llm_calls_saved(reason: str) -> None:
    """Phase H·W1：确定性 fast-path 跳过的 LLM 意图分类调用数（reason 有界枚举）。"""
    LLM_CALLS_SAVED.labels(reason=reason or "unknown").inc()


#: T8：路由缓存结果的封闭枚举。调用点只有
#: ``agentix/application/agents/router.py``（``hit``/``miss`` 来自命中判定，
#: ``error`` 只用于"读写或重建抛异常且已 fail-open 降级为未命中"）。命中率 =
#: ``hit / (hit + miss)``——``error`` 不计入分母，否则缓存故障会伪装成"命中率下降"。
ROUTING_CACHE_RESULT_VALUES = frozenset({"hit", "miss", "error"})


def record_routing_cache(result: str) -> None:
    """T8：进程内路由结果缓存的一次判定（hit / miss / error，有界枚举）。"""
    ROUTING_CACHE.labels(result=_bounded_label(result, ROUTING_CACHE_RESULT_VALUES)).inc()


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
    """记录审批决策（值域维护者 2026-10-27 裁决，六值）。

    ``decision`` 只允许 ``{approved, rejected, edited, resumed, auto,
    bypass}``：进环/检索侧写 ``approved``/``rejected``，桥侧人工裁决经
    bucket 映射写四值之一，``auto``/``bypass`` 来自桥侧自动放行
    （逐请求 auto 与 PermissionMode.BYPASS 模式级豁免，治理含义不同）。
    ``target_type`` 实测写入值：``tool``（桥/进环）与检索侧
    ``document`` / ``kb``；``tenant`` 只流向 write_audit（审计），从不进
    本指标，故不列入——help/docstring 是下游取证的依据，声称比实现宽
    会放行一条根本没落下的取值。指标层对空输入兜底写 ``unknown``，但两侧
    可触达性不同：``decision`` 侧经调用点闭合可证为上述六值（桥/进环传常量或
    bucket 映射值、检索侧传 ``approved``/``rejected``），兜底不可触达；
    ``target_type`` 侧桥/进环为常量 ``tool``，检索侧透传 ``ap.target_type``——
    本仓创建点只写 ``document``/``kb``，但模型字段是 ``Field(description=...)``
    （描述非约束）、仓储层 ``create_approval`` 也不校验取值，故库里存在
    ``target_type=""`` 的历史行时兜底会被触达 ⇒ 序列出现 ``unknown``
    应先查库行，而不是怀疑调用点。
    """
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


def record_checkpointer_upgrade(result: str) -> None:
    """T22：记录一次 checkpointer 升级重试的结果（result: ok / fail，封闭枚举）。

    调用点只有 `agentix/infrastructure/persistence/checkpointer.py` 的
    `retry_upgrade_checkpointer`，且只传两个字面量——词表外取值不可达，这里不另设
    兜底映射（在指标层改口会把"调用点传错了"这件事一起吞掉）。
    """
    CHECKPOINTER_UPGRADE.labels(result=result).inc()


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
    # T6：先置本流兜底事实再动计数器（计数器故障不丢事实，事实由调用方兜异常）——
    # 本函数只在回退链自身判定点被调用（model_gateway.py 的 `if errors:` 分支），
    # 因此「是否走了兜底」在全仓只有一个源（纪律 6 ④）。
    fact = _gateway_fallback_fact.get()
    if fact is not None:
        fact.observed = True
    GATEWAY_FALLBACK.labels(
        primary_model=primary_model or "unknown",
        fallback_model=fallback_model or "unknown",
    ).inc()


# 路由兜底 reason 的有界取值；表外值统一落 unknown（标签值有界 = 指标基数有界）
_ROUTER_FALLBACK_REASONS = frozenset({"exception", "empty", "invalid_type"})


def record_router_fallback(reason: str) -> None:
    """R-P3：路由 L1 兜底 +1（reason 有界，表外值归 ``unknown``）。

    reason 语义：
    - ``exception``：分类那次 LLM 调用抛错/超时 ⇒ 直接用兜底 Router；
    - ``empty``：模型返回但 ``type`` 为空 ⇒ 判据降级为启发式/兜底；
    - ``invalid_type``：``type`` 不在白名单 ⇒ 同上，但可归因到"模型给了越界类型"。
    """
    r = reason if reason in _ROUTER_FALLBACK_REASONS else "unknown"
    ROUTER_FALLBACK.labels(reason=r).inc()


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
