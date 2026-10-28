"""
可观测性模块

提供 Prometheus 指标收集和 OpenTelemetry 链路追踪能力。

使用方式：
    from agentix.observability import (
        init_observability,
        record_request,
        record_llm_usage,
        trace_span,
        traced,
    )
"""

from agentix.observability.metrics import (
    # HTTP 指标
    REQUEST_COUNT,
    REQUEST_LATENCY,
    record_request,
    # RAG 指标
    RETRIEVAL_COUNT,
    RETRIEVAL_LATENCY,
    record_retrieval,
    # LLM 指标
    LLM_TOKEN_USAGE,
    LLM_REQUEST_LATENCY,
    record_llm_usage,
    record_index_job,
    # Agent 指标
    AGENT_TOOL_CALLS,
    record_tool_call,
    # 缓存指标
    CACHE_HITS,
    CACHE_MISSES,
    record_cache_hit,
    record_cache_miss,
    # 企业化 P1：降级/熔断/并发/injection 指标
    record_degraded,
    record_router_fallback,
    record_circuit_state,
    record_circuit_transition,
    record_llm_concurrency_occupied,
    record_llm_concurrency_released,
    record_llm_concurrency_rejected,
    record_prompt_injection,
    # 工具函数
    track_latency,
    timed,
    get_metrics,
    get_metrics_content_type,
    PROMETHEUS_AVAILABLE,
)

from agentix.observability.tracing import (
    init_tracing,
    shutdown_tracing,
    trace_span,
    sync_trace_span,
    traced,
    trace_rag_pipeline,
    tracer,
    OTEL_AVAILABLE,
)

from agentix.observability.context import (
    bind_request_id,
    current_request_id,
    reset_request_id,
    bind_trace_id,
    current_trace_id,
    reset_trace_id,
)

from agentix.observability.logging_config import (
    JsonFormatter,
    setup_logging,
)

from agentix.observability.spans import (
    span,
    sync_span,
)

from agentix.observability.trajectory import (
    TrajectoryRecorder,
    current_recorder,
    reset_recorder,
    set_recorder,
)


def init_observability(
    service_name: str = "agentix_retrieval",
    service_version: str = "1.0.0",
    otlp_endpoint: str = None,
    console_export: bool = False,
):
    """初始化可观测性（指标 + 追踪）
    
    Args:
        service_name: 服务名称
        service_version: 服务版本
        otlp_endpoint: OTLP 导出端点
        console_export: 是否输出追踪到控制台
    """
    init_tracing(
        service_name=service_name,
        service_version=service_version,
        otlp_endpoint=otlp_endpoint,
        console_export=console_export,
    )


__all__ = [
    # 初始化
    "init_observability",
    "init_tracing",
    "shutdown_tracing",
    # 指标
    "REQUEST_COUNT",
    "REQUEST_LATENCY",
    "record_request",
    "RETRIEVAL_COUNT",
    "RETRIEVAL_LATENCY",
    "record_retrieval",
    "LLM_TOKEN_USAGE",
    "LLM_REQUEST_LATENCY",
    "record_llm_usage",
    "record_index_job",
    "AGENT_TOOL_CALLS",
    "record_tool_call",
    "CACHE_HITS",
    "CACHE_MISSES",
    "record_cache_hit",
    "record_cache_miss",
    "record_degraded",
    "record_router_fallback",
    "record_circuit_state",
    "record_circuit_transition",
    "record_llm_concurrency_occupied",
    "record_llm_concurrency_released",
    "record_llm_concurrency_rejected",
    "record_prompt_injection",
    "track_latency",
    "timed",
    "get_metrics",
    "get_metrics_content_type",
    "PROMETHEUS_AVAILABLE",
    # 追踪
    "trace_span",
    "sync_trace_span",
    "traced",
    "trace_rag_pipeline",
    "tracer",
    "OTEL_AVAILABLE",
    # request-id / trace-id 上下文
    "bind_request_id",
    "current_request_id",
    "reset_request_id",
    "bind_trace_id",
    "current_trace_id",
    "reset_trace_id",
    # JSON 日志
    "JsonFormatter",
    "setup_logging",
    # span 网关
    "span",
    "sync_span",
    # Trajectory
    "TrajectoryRecorder",
    "current_recorder",
    "reset_recorder",
    "set_recorder",
]
