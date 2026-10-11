"""
可观测性 - OpenTelemetry 分布式链路追踪

提供全链路追踪能力，支持追踪 RAG 管道每个阶段的耗时和状态。

使用方式：
    from agentix.observability.tracing import tracer, trace_span, init_tracing
    
    # 初始化（应用启动时调用一次）
    init_tracing(service_name="agentix_retrieval")
    
    # 使用装饰器追踪
    @trace_span("retrieve_documents")
    async def retrieve(query: str) -> list:
        ...
    
    # 使用上下文管理器追踪
    async with trace_span("generate_answer") as span:
        span.set_attribute("query.length", len(query))
        answer = await generate(query, docs)
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager, contextmanager
from functools import wraps
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# 尝试导入 OpenTelemetry，不可用时降级
try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.trace import Status, StatusCode
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    
    OTEL_AVAILABLE = True
    logger.info("OpenTelemetry tracing enabled")

except ImportError:
    OTEL_AVAILABLE = False
    logger.warning("opentelemetry not installed, tracing disabled")


# ============================================================
# 降级实现（OpenTelemetry 不可用时）
# ============================================================

class _NoopSpan:
    """空操作 Span，用于降级"""
    def set_attribute(self, key: str, value: Any): pass
    def set_status(self, status): pass
    def add_event(self, name: str, attributes: dict = None): pass
    def record_exception(self, exception: Exception): pass
    def end(self): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass


class _NoopTracer:
    """空操作 Tracer，用于降级"""
    def start_span(self, name: str, **kwargs): return _NoopSpan()
    def start_as_current_span(self, name: str, **kwargs): return _NoopSpan()


# 全局 tracer 实例
tracer = _NoopTracer()
_tracer_provider = None


# ============================================================
# 初始化
# ============================================================

def init_tracing(
    service_name: str = "agentix_retrieval",
    service_version: str = "1.0.0",
    otlp_endpoint: Optional[str] = None,
    console_export: bool = False,
) -> None:
    """初始化 OpenTelemetry 链路追踪
    
    Args:
        service_name: 服务名称
        service_version: 服务版本
        otlp_endpoint: OTLP 导出端点（如 http://localhost:4317）
        console_export: 是否输出到控制台（调试用）
    """
    global tracer, _tracer_provider
    
    if not OTEL_AVAILABLE:
        logger.warning("OpenTelemetry not available, using noop tracer")
        return
    
    # 从环境变量读取配置
    otlp_endpoint = otlp_endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    console_export = console_export or os.getenv("OTEL_CONSOLE_EXPORT", "false").lower() in ("1", "true")
    
    # 创建资源
    resource = Resource.create({
        "service.name": service_name,
        "service.version": service_version,
    })
    
    # 创建 TracerProvider
    _tracer_provider = TracerProvider(resource=resource)
    
    # 添加导出器
    if otlp_endpoint:
        try:
            otlp_exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
            _tracer_provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
            logger.info("OTLP exporter configured: %s", otlp_endpoint)
        except Exception as e:
            logger.warning("Failed to configure OTLP exporter: %s", e)
    
    if console_export:
        console_exporter = ConsoleSpanExporter()
        _tracer_provider.add_span_processor(BatchSpanProcessor(console_exporter))
        logger.info("Console exporter enabled")
    
    # 设置全局 tracer
    trace.set_tracer_provider(_tracer_provider)
    tracer = trace.get_tracer(service_name)
    
    logger.info("Tracing initialized: service=%s, version=%s", service_name, service_version)


def shutdown_tracing() -> None:
    """关闭追踪（应用退出时调用）"""
    global _tracer_provider
    if _tracer_provider and hasattr(_tracer_provider, "shutdown"):
        _tracer_provider.shutdown()
        logger.info("Tracing shutdown")


# ============================================================
# 便捷工具
# ============================================================

@asynccontextmanager
async def trace_span(name: str, attributes: dict = None):
    """异步上下文管理器：创建追踪 Span
    
    用法：
        async with trace_span("retrieve_documents", {"query": query}) as span:
            docs = await retrieve(query)
            span.set_attribute("docs.count", len(docs))
    """
    if not OTEL_AVAILABLE:
        yield _NoopSpan()
        return
    
    with tracer.start_as_current_span(name) as span:
        if attributes:
            for key, value in attributes.items():
                span.set_attribute(key, value)
        
        start_time = time.perf_counter()
        try:
            yield span
            span.set_status(Status(StatusCode.OK))
        except Exception as e:
            span.set_status(Status(StatusCode.ERROR, str(e)))
            span.record_exception(e)
            raise
        finally:
            duration = time.perf_counter() - start_time
            span.set_attribute("duration_ms", round(duration * 1000, 2))


@contextmanager
def sync_trace_span(name: str, attributes: dict = None):
    """同步上下文管理器：创建追踪 Span"""
    if not OTEL_AVAILABLE:
        yield _NoopSpan()
        return
    
    with tracer.start_as_current_span(name) as span:
        if attributes:
            for key, value in attributes.items():
                span.set_attribute(key, value)
        
        start_time = time.perf_counter()
        try:
            yield span
            span.set_status(Status(StatusCode.OK))
        except Exception as e:
            span.set_status(Status(StatusCode.ERROR, str(e)))
            span.record_exception(e)
            raise
        finally:
            duration = time.perf_counter() - start_time
            span.set_attribute("duration_ms", round(duration * 1000, 2))


def traced(name: str = None):
    """装饰器：自动追踪函数执行
    
    用法：
        @traced("retrieve_documents")
        async def retrieve(query: str) -> list:
            ...
    """
    def decorator(func: Callable) -> Callable:
        span_name = name or func.__name__
        
        @wraps(func)
        async def wrapper(*args, **kwargs):
            async with trace_span(span_name) as span:
                span.set_attribute("function", func.__name__)
                span.set_attribute("module", func.__module__)
                return await func(*args, **kwargs)
        
        return wrapper
    return decorator


# ============================================================
# GenAI 语义属性（OTel GenAI semconv，Phase H · W7）
# ============================================================

# 语义约定键名（字符串常量，零新增依赖——otel sdk 为既有硬依赖）
GENAI_SYSTEM = "gen_ai.system"
GENAI_OPERATION_NAME = "gen_ai.operation.name"
GENAI_REQUEST_MODEL = "gen_ai.request.model"
GENAI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GENAI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"


def genai_span_name(model: Optional[str] = None, operation: str = "chat") -> str:
    """LLM span 名按 semconv：``{operation} {model}``（如 ``chat deepseek-v4-flash``）。"""
    if model:
        return f"{operation} {model}"
    return operation


def infer_genai_system(model: Optional[str] = None) -> str:
    """按模型名推断 GenAI provider（当前部署：DeepSeek 模型 / OpenAI 兼容方言）。"""
    if model and "deepseek" in model.lower():
        return "deepseek"
    return "openai"


def apply_genai_request(
    span: Any,
    *,
    model: Optional[str] = None,
    operation: str = "chat",
    system: Optional[str] = None,
) -> None:
    """请求侧 ``gen_ai.*`` 属性；观测设施异常绝不影响主链路。"""
    if span is None:
        return
    try:
        span.set_attribute(GENAI_OPERATION_NAME, operation)
        if model:
            span.set_attribute(GENAI_REQUEST_MODEL, model)
        span.set_attribute(GENAI_SYSTEM, system or infer_genai_system(model))
    except Exception:  # noqa: BLE001 - 观测故障 fail-open
        logger.debug("apply_genai_request failed", exc_info=True)


def apply_genai_usage(
    span: Any,
    *,
    usage: Any = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
) -> None:
    """用量侧 ``gen_ai.usage.*``——兼容 langchain ``usage_metadata`` 与
    openai ``token_usage`` 两种键名（prompt/completion_tokens）。

    缺失/None 不写属性（不臆造 0，不写 None）。
    """
    if span is None:
        return
    try:
        if isinstance(usage, dict):
            if input_tokens is None:
                input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
            if output_tokens is None:
                output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
        if input_tokens is not None:
            span.set_attribute(GENAI_USAGE_INPUT_TOKENS, int(input_tokens))
        if output_tokens is not None:
            span.set_attribute(GENAI_USAGE_OUTPUT_TOKENS, int(output_tokens))
    except Exception:  # noqa: BLE001 - 观测故障 fail-open
        logger.debug("apply_genai_usage failed", exc_info=True)


# ============================================================
# RAG 管道专用追踪
# ============================================================

async def trace_rag_pipeline(query: str, kb_id: str):
    """追踪完整 RAG 管道
    
    用法：
        async with trace_rag_pipeline(query, kb_id) as pipeline:
            docs = await pipeline.retrieve()
            answer = await pipeline.generate(docs)
    """
    class RAGPipelineTracer:
        def __init__(self, span):
            self.span = span
            self.stage_start = None
        
        async def __aenter__(self):
            return self
        
        async def __aexit__(self, *args):
            pass
        
        async def retrieve(self, func, *args, **kwargs):
            """追踪检索阶段"""
            async with trace_span("rag.retrieve") as span:
                span.set_attribute("kb_id", kb_id)
                span.set_attribute("query.length", len(query))
                result = await func(*args, **kwargs)
                span.set_attribute("docs.count", len(result) if result else 0)
                return result
        
        async def rerank(self, func, *args, **kwargs):
            """追踪重排阶段"""
            async with trace_span("rag.rerank") as span:
                result = await func(*args, **kwargs)
                span.set_attribute("reranked.count", len(result) if result else 0)
                return result
        
        async def generate(self, func, *args, **kwargs):
            """追踪生成阶段"""
            async with trace_span("rag.generate") as span:
                result = await func(*args, **kwargs)
                span.set_attribute("answer.length", len(result) if result else 0)
                return result
    
    async with trace_span("rag.pipeline", {"kb_id": kb_id, "query.length": len(query)}) as span:
        yield RAGPipelineTracer(span)
