"""组合根装配：把生命周期 emit 装进全局 ToolExecutor（Phase 21）。

为什么注入而不是 executor 直接 import 本包：``capabilities`` 永不反向
import ``agentix.agent``（分层不变量）。executor 只暴露中性的
``set_observation_hooks(before=..., after=...)`` 回调口，事件名与
``emit`` 语义在这里（agent 侧）绑定——与 Phase 16
``hitl_bridge.install_executor_hook()`` 同款组合根模式。

调用方（``main.py::_startup``）只在 ``settings.HOOKS_ENABLED=true`` 时调用；
即便漏判，``emit`` 内部仍 gate 一次 settings（双保险）。
"""

from __future__ import annotations

from typing import Any, Optional

from agentix.agent.hooks.events import ON_TOOL_AFTER, ON_TOOL_BEFORE
from agentix.agent.hooks.registry import emit

__all__ = ["install_tool_hooks"]


def install_tool_hooks() -> None:
    """给全局 ``ToolExecutor`` 安装 before/after 观测回调（幂等，覆盖式）。"""
    from agentix.tools.gateway.executor import get_tool_executor

    def _before(tool_name: str, arguments: dict) -> None:
        emit(ON_TOOL_BEFORE, {"tool_name": tool_name, "arguments": arguments})

    def _after(tool_name: str, result: Optional[Any]) -> None:
        payload: dict = {"tool_name": tool_name, "result": result}
        if result is not None:
            payload["success"] = getattr(result, "success", None)
            payload["error"] = getattr(result, "error", None)
            payload["call_id"] = getattr(result, "call_id", None)
        emit(ON_TOOL_AFTER, payload)

    get_tool_executor().set_observation_hooks(before=_before, after=_after)
