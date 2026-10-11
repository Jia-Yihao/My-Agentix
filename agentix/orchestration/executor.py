"""Executor：把 PlanStep 映射到可调用工具（经注入的 tool 映射表）。

零 infrastructure import：工具实现由上层注册为 ``Callable`` / awaitable。

Phase 18（L1 Budget）：工具结果出层即过 ``result_limiter``——大输出确定性
截断/分页且**显式告知**（截断信息记入 ``Observation.meta['limited']``）；
未触限的结果与既有行为逐字节一致（Strangler 零变化）。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Tuple, Union

from agentix.domain.agent.types import (
    ContextSnapshot,
    FailureLayer,
    Observation,
    Plan,
    PlanStep,
    TaskSpec,
)

try:  # L1 Budget：出层限量（import 失败 → 降级为无界，行为同既有）
    from agentix.tools.gateway.result_limiter import limit_value as _limit_value
except ImportError:  # pragma: no cover — 降级路径
    _limit_value = None  # type: ignore[assignment]

try:  # P4-4.2：重复结果 stub（import 失败 → 降级为不去重，行为同既有）
    from agentix.tools.gateway.result_limiter import (
    get_result_deduplicator as _get_deduplicator,
)
except ImportError:  # pragma: no cover — 降级路径
    _get_deduplicator = None  # type: ignore[assignment]

__all__ = ["MappingStepExecutor", "ToolCallable"]

ToolCallable = Callable[..., Union[Any, Awaitable[Any]]]


class MappingStepExecutor:
    """``tools``: name → callable。step.tool 缺失或未注册 → Observation(ok=False)。"""

    def __init__(self, tools: Optional[Mapping[str, ToolCallable]] = None) -> None:
        self._tools: Dict[str, ToolCallable] = dict(tools or {})

    def register(self, name: str, fn: ToolCallable) -> None:
        self._tools[name] = fn

    async def execute(
        self,
        task: TaskSpec,
        plan: Plan,
        step: PlanStep,
        context: ContextSnapshot,
    ) -> Observation:
        name = step.tool or step.action
        fn = self._tools.get(name)
        if fn is None:
            return Observation(
                step_id=step.id,
                content=f"tool_not_found: {name}",
                ok=False,
                failure_layer=FailureLayer.TOOL,
                error_code="tool_not_found",
            )
        try:
            result = fn(task=task, step=step, context=context)
            if hasattr(result, "__await__"):
                result = await result  # type: ignore[misc]
            # P4-4.2：重复结果 stub（TOOL_GUARDRAILS_ENABLED 默认关 ⇒ 零调用）——
            # 工具已真实执行，只折叠进入上下文的表示；轮询不豁免。
            if _get_deduplicator is not None:
                from agentix.config.settings import settings as _cfg

                if bool(getattr(_cfg, "TOOL_GUARDRAILS_ENABLED", False)):
                    stub = _get_deduplicator().observe(name, result)
                    if stub is not None:
                        return Observation(
                            step_id=step.id,
                            content=stub,
                            ok=True,
                            meta={"deduped": True, "tool": name},
                        )
            content, limit_meta = _limit_result(result)
            return Observation(step_id=step.id, content=content, ok=True, meta=limit_meta)
        except Exception as exc:  # noqa: BLE001 — 归一层，loop 不再 catch 业务异常
            return Observation(
                step_id=step.id,
                content=f"{type(exc).__name__}: {exc}",
                ok=False,
                failure_layer=classify_exception(exc),
                error_code="tool_exec_failed",
            )


def _limit_result(result: Any) -> Tuple[str, Dict[str, Any]]:
    """L1 Budget：超限才动（截断/分页 + 显式提示）；未超限保持既有形态。"""
    if _limit_value is None:  # pragma: no cover — 降级路径
        return (result if isinstance(result, str) else _stable_repr(result)), {}
    try:
        limited = _limit_value(result)
    except Exception:  # noqa: BLE001 — 限量失败不得拖垮工具执行
        return (result if isinstance(result, str) else _stable_repr(result)), {}
    if not (limited.meta.truncated or limited.meta.has_more):
        return (result if isinstance(result, str) else _stable_repr(result)), {}
    return _stable_repr(limited.with_notice()), {"limited": limited.meta.to_dict()}


def _stable_repr(value: Any) -> str:
    if isinstance(value, dict):
        return "\n".join(f"{k}={value[k]}" for k in sorted(value))
    return str(value)


def classify_exception(exc: BaseException) -> FailureLayer:
    """书 §5.7 四层故障粗分（异常消息启发式）。"""
    msg = f"{type(exc).__name__}: {exc}".lower()
    if any(k in msg for k in ("timeout", "timed out", "cancelled", "budget", "max_steps")):
        return FailureLayer.CONTROL_FLOW
    if any(k in msg for k in ("context", "token", "overflow", "prompt too")):
        return FailureLayer.CONTEXT
    if any(
        k in msg
        for k in (
            "rate limit",
            "llm",
            "model",
            "api key",
            "503",
            "429",
            "502",
            "504",
            "upstream",
        )
    ):
        return FailureLayer.API
    return FailureLayer.TOOL
