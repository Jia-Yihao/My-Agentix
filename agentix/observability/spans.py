"""统一 span 网关：业务代码只允许用 ``span()`` / ``sync_span()``（业务内唯一 span 入口）。

- 转发 ``agentix.observability.tracing`` 的 OTel 封装；OTel 未安装时
  ``trace_span`` / ``sync_trace_span`` 为 noop，零开销降级；
- 同时把步骤写入当前 ``TrajectoryRecorder``（若已 start），供落盘回放。
未来安装 OTel 无需改业务代码。

另有 ``genai_span()``（LLM 调用专用，``chat {model}`` 命名 +
``gen_ai.*`` 语义属性）与 ``apply_genai_usage`` 再导出——genai_span **不写
trajectory**（LLM 调用轨迹已由 llm_factory._record_llm_trajectory 与检索域
usage 记录覆盖，避免一步双记）。

三个决策属性 helper（``record_route_decision`` /
``record_model_used`` / ``record_resume_from_checkpoint``），消费点分别在 ``application/agents/router.py``（``analyze_and_route_query``）、
``retrieval/usecases/chat_agent_tools.py``（两处）、``orchestration/langgraph/lg_builder.py``
与 ``application/services/breakpoint_resume.py``（``resume_thread``）；只把**有界可枚举的判定值**写进当前 span（约定 3：理由文本与高基数
内容不落），且无 span / OTel 抛异常时**静默降级**，绝不向上抛（约定 2）。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, Iterator

from agentix.observability.tracing import (
    GENAI_RESPONSE_MODEL,
    apply_genai_usage,
    sync_trace_span,
    trace_span,
)

logger = logging.getLogger(__name__)

__all__ = [
    "span",
    "sync_span",
    "genai_span",
    "apply_genai_usage",
    "ROUTE_DECISION_ATTR",
    "RESUME_ATTR",
    "record_route_decision",
    "record_model_used",
    "record_resume_from_checkpoint",
]


def _clean_attributes(attributes: dict) -> dict | None:
    attrs = {k: v for k, v in attributes.items() if v is not None}
    return attrs or None


@asynccontextmanager
async def span(name: str, **attributes) -> AsyncIterator:
    """异步 span：``async with span("rag.retrieve", kb_id=kb_id):``"""
    attrs = _clean_attributes(attributes)
    from agentix.observability.trajectory import current_recorder

    recorder = current_recorder()
    step = recorder.begin_step(name) if recorder else None
    try:
        async with trace_span(name, attrs) as otel_span:
            yield otel_span
    except BaseException:
        if recorder is not None and step is not None:
            recorder.fail_step(step, attributes=attrs)
        raise
    else:
        if recorder is not None and step is not None:
            recorder.end_step(step, attributes=attrs)


@asynccontextmanager
async def genai_span(
    model: str | None = None,
    *,
    operation: str = "chat",
    system: str | None = None,
) -> AsyncIterator:
    """LLM 调用专用 span：名 ``{operation} {model}``，预置
    ``gen_ai.operation.name`` / ``gen_ai.request.model`` / ``gen_ai.system``；
    用量经 ``apply_genai_usage(span, usage=...)`` 在调用返回后补写。

    与 ``span()`` 的差异：**不写 trajectory**（LLM 调用轨迹已由
    ``llm_factory._record_llm_trajectory`` 与检索域 usage 记录覆盖）。
    """
    from agentix.observability.tracing import apply_genai_request, genai_span_name

    async with trace_span(genai_span_name(model, operation)) as otel_span:
        apply_genai_request(otel_span, model=model, operation=operation, system=system)
        yield otel_span


@contextmanager
def sync_span(name: str, **attributes) -> Iterator:
    """同步 span：``with sync_span("index.build", kb_id=kb_id):``"""
    attrs = _clean_attributes(attributes)
    from agentix.observability.trajectory import current_recorder

    recorder = current_recorder()
    step = recorder.begin_step(name) if recorder else None
    try:
        with sync_trace_span(name, attrs) as otel_span:
            yield otel_span
    except BaseException:
        if recorder is not None and step is not None:
            recorder.fail_step(step, attributes=attrs)
        raise
    else:
        if recorder is not None and step is not None:
            recorder.end_step(step, attributes=attrs)


# ============================================================
# 决策属性 helper（纯新增——消费点在各调用方）
# ============================================================

ROUTE_DECISION_ATTR = "agentix.route.decision"
RESUME_ATTR = "agentix.resume_from_checkpoint"


def _current_span():
    """取当前活动 span；无活动 span / span 未记录 / OTel 未安装时返回 None。

    三条纪律决定它必须存在且只有一个：
    - 三个 record_* helper 对"有没有 span"的判据同源（约定 6 ④），不许各自
      探测一次 opentelemetry；
    - OTel 未安装时本仓的 ``_NoopSpan`` 只随 with 上下文传递、不上 current-span
      通道，这里返回 None 即预期降级（不是漏测）；
    - 它是测试唯一可替换的接缝（假 span / 坏 span / 接缝本身抛）。
    """
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    span = trace.get_current_span()
    if span is None or not span.is_recording():
        return None
    return span


def record_route_decision(route_type: str) -> None:
    """当前 span 记路由决策值 ``agentix.route.decision``。

    只落有界判定值（路由意图枚举，见 7 类词表守卫），理由文本不落
    （约定 3）。空串 / 无 span ⇒ 静默 no-op；异常一律吞并 debug 留痕——
    观测设施失败不得让一次正常问答失败（§审查重点第 47 行）。
    """
    try:
        if not route_type:
            return
        span = _current_span()
        if span is None:
            return
        span.set_attribute(ROUTE_DECISION_ATTR, route_type)
    except Exception:  # noqa: BLE001 - 观测故障 fail-open：降级为不记属性，logger.debug 留痕
        logger.debug("record_route_decision failed", exc_info=True)


def record_model_used(model_used: str, *, primary_model: str) -> None:
    """当前 span 记实际服务方 ``gen_ai.response.model`` = model_used。

    与 primary_model 相同也写：该属性的语义是"实际服务方"，不是"是否发生了
    回退"。primary_model 仅作调用点语义显式化（消费点不许自备比较逻辑造出
    第二个判定源，约定 6 ④），本身不写属性——请求侧模型名已由
    ``genai_span`` 预置 ``gen_ai.request.model``。空串 / 无 span / 异常 ⇒ no-op。
    """
    try:
        if not model_used:
            return
        span = _current_span()
        if span is None:
            return
        span.set_attribute(GENAI_RESPONSE_MODEL, model_used)
    except Exception:  # noqa: BLE001 - 观测故障 fail-open：降级为不记属性，logger.debug 留痕
        logger.debug("record_model_used failed", exc_info=True)


def record_resume_from_checkpoint(*, thread_id_present: bool) -> None:
    """恢复点在当前 span 记 ``agentix.resume_from_checkpoint`` = True。

    仅 thread_id_present 为真时写；为假**不写该属性**（属性缺席即"不是从
    检查点恢复"，写 False 会让无恢复的绝大多数 span 都背上一个噪声属性）。
    判据口径：config["configurable"]["thread_id"] 是否非空。
    """
    try:
        if not thread_id_present:
            return
        span = _current_span()
        if span is None:
            return
        span.set_attribute(RESUME_ATTR, True)
    except Exception:  # noqa: BLE001 - 观测故障 fail-open：降级为不记属性，logger.debug 留痕
        logger.debug("record_resume_from_checkpoint failed", exc_info=True)
