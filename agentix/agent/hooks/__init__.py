"""生命周期 Hooks（Phase 21，书 §5.6）：五类旁路事件的注册与触发。

默认 ``settings.HOOKS_ENABLED=false``：组合根零接线 + ``emit`` 首行 gate，
主路径零调用零开销（Strangler flag-off 字节等价）。

用法::

    from agentix.agent.hooks import ON_STEP_END, register
    register(ON_STEP_END, lambda payload: ...)
    from agentix.agent.hooks import emit  # 或走 install_tool_hooks 装配 executor
"""

from __future__ import annotations

from agentix.agent.hooks.events import (
    EVENT_TYPES,
    ON_ERROR,
    ON_RUN_END,
    ON_STEP_END,
    ON_TOOL_AFTER,
    ON_TOOL_BEFORE,
)
from agentix.agent.hooks.registry import clear_hooks, emit, hook_events, register

__all__ = [
    "EVENT_TYPES",
    "ON_ERROR",
    "ON_RUN_END",
    "ON_STEP_END",
    "ON_TOOL_AFTER",
    "ON_TOOL_BEFORE",
    "clear_hooks",
    "emit",
    "hook_events",
    "register",
    "install_tool_hooks",
]


def install_tool_hooks() -> None:
    """惰性转出（避免 ``__init__`` import 期碰 capabilities）。"""
    from agentix.agent.hooks.install import install_tool_hooks as _install

    _install()
