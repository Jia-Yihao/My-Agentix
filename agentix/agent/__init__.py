"""Agent Core 2.0 — Harness 实现包（Phase 2+）。

依赖方向：``agentix.agent`` → ``domain``（类型/端口）；
LangGraph **仅**允许出现在 ``agentix.agent.graph``。
本包不依赖 ``infrastructure`` / ``interfaces`` 具体实现（经 Protocol 注入）。

M2 起 ``AgentLoop`` 实现在 ``agentix.orchestration.orchestrator``；M6 清尾后旧路径
``agent.core.loop`` 转发 shim 已删除，此处仍**惰性**再导出（PEP 562）：若 eager
``from ... orchestrator import``，当 ``orchestration.orchestrator`` 先于本包加载、
其顶部 ``from agentix.agent.hooks import ...`` 触发本包 ``__init__`` 时会
回拉半初始化的 orchestrator 炸 AttributeError。
"""
from __future__ import annotations

import importlib
from typing import Any

__all__ = ["AgentLoop", "LoopResult"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        mod = importlib.import_module("agentix.orchestration.orchestrator")
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
