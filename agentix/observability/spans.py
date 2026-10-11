"""统一 span 网关：业务代码只允许用 ``span()`` / ``sync_span()``（AGENTS 硬规则 12）。

- 转发 ``agentix.observability.tracing`` 的 OTel 封装；OTel 未安装时
  ``trace_span`` / ``sync_trace_span`` 为 noop，零开销降级；
- 同时把步骤写入当前 ``TrajectoryRecorder``（若已 start），供落盘回放。
未来安装 OTel 无需改业务代码。

Phase H·W7 增补：``genai_span()``（LLM 调用专用，``chat {model}`` 命名 +
``gen_ai.*`` 语义属性）与 ``apply_genai_usage`` 再导出——genai_span **不写
trajectory**（LLM 调用轨迹已由 llm_factory._record_llm_trajectory 与检索域
usage 记录覆盖，避免一步双记）。
"""
from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, Iterator

from agentix.observability.tracing import (
    apply_genai_usage,
    sync_trace_span,
    trace_span,
)

__all__ = ["span", "sync_span", "genai_span", "apply_genai_usage"]


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
    """LLM 调用专用 span（Phase H·W7）：名 ``{operation} {model}``，预置
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
