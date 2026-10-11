"""生命周期事件常量（Phase 21，书 §5.6 / §2.7 Hooks）。

五类旁路事件（同步触发、进程内语义，不跨进程不落盘）::

    on_tool_before   工具执行开始（ToolExecutor 全部闸门放行后、真正执行前）
    on_tool_after    工具执行结果出口（ToolResult；与 before 成对）
    on_step_end      AgentLoop 单步观测入列后
    on_run_end       AgentLoop 一轮终态产出（finally）
    on_error         loop 边界兜底（context/plan/step/verify 四相位）

事件名 = 字符串常量：capabilities 侧只暴露 ``before/after`` 回调签名、
不认识这些常量（分层：事件语义归属 agent 侧，能力层零反向依赖）。
"""

from __future__ import annotations

from typing import Tuple

__all__ = [
    "ON_TOOL_BEFORE",
    "ON_TOOL_AFTER",
    "ON_STEP_END",
    "ON_RUN_END",
    "ON_ERROR",
    "EVENT_TYPES",
]

ON_TOOL_BEFORE = "on_tool_before"
ON_TOOL_AFTER = "on_tool_after"
ON_STEP_END = "on_step_end"
ON_RUN_END = "on_run_end"
ON_ERROR = "on_error"

#: 合法事件全集（注册未知事件名直接 ValueError，防拼写错静默吞）
EVENT_TYPES: Tuple[str, ...] = (
    ON_TOOL_BEFORE,
    ON_TOOL_AFTER,
    ON_STEP_END,
    ON_RUN_END,
    ON_ERROR,
)
