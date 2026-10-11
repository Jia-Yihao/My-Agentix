"""组合根装配：把 ``read_skill`` / ``create_skill_draft`` 处理器装进全局
ToolExecutor（Phase 21 + P6-6.3）。

职责切分：
- **声明**（spec + LLM schema）在 ``capabilities/tools/registry_init.py``，
  由 ``settings.SKILLS_ENABLED`` gate（flag-off 不进声明清单）；
- **实现**（handler + 正文读取 / 草稿写入）在本包；
- 本函数只做接线：``ensure_registered()``（保证 spec 先入 registry）+
  ``register_handler``。``main.py::_startup`` 仅在 flag-on 时调用。
"""

from __future__ import annotations

from agentix.agent.skills.registry import read_skill

__all__ = ["install_read_skill_tool", "install_skill_draft_tool"]


def install_read_skill_tool() -> None:
    """注册 ``read_skill`` 处理器（幂等；依赖 flag-on 声明清单已有 spec）。"""
    from agentix.config.settings import settings

    if not settings.SKILLS_ENABLED:
        return  # belt-and-suspenders：调用方已判 flag，这里再判一次
    from agentix.tools.gateway.executor import get_tool_executor
    from agentix.tools.registry_init import ensure_registered

    ensure_registered()  # spec 先入 registry（executor 执行点要求 spec 存在）

    def _handler(args: dict):
        # 工具层永不抛：未知名字 → found=False + available_skills 回执
        return read_skill(args.get("name", ""))

    get_tool_executor().register_handler("read_skill", _handler)


def install_skill_draft_tool() -> None:
    """注册 ``create_skill_draft`` 处理器（P6-6.3；同 SKILLS_ENABLED gate）。"""
    from agentix.config.settings import settings

    if not settings.SKILLS_ENABLED:
        return  # belt-and-suspenders：调用方已判 flag，这里再判一次
    from agentix.agent.skills.learn import create_skill_draft
    from agentix.tools.gateway.executor import get_tool_executor
    from agentix.tools.registry_init import ensure_registered

    ensure_registered()  # spec 先入 registry（executor 执行点要求 spec 存在）

    def _handler(args: dict):
        # 工具层永不抛：校验失败 → created=False + errors 回执
        return create_skill_draft(args)

    get_tool_executor().register_handler("create_skill_draft", _handler)
