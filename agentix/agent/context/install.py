"""组合根装配：把 ``read_agents_file`` 处理器装进全局 ToolExecutor（F1b2·M-P1-4）。

职责切分（与 ``skills/install.py`` 同款）：

- **声明**（spec + LLM schema）在 ``agentix/tools/adapters/agents_file_read.py``，
  由 ``settings.AGENTS_CASCADE_ENABLED`` gate（flag-off 不进声明清单）；
- **实现**（向上查找 + 读取截断）在本模块，复用
  ``instructions.find_project_instructions`` / ``read_instruction_text``；
- 本函数只做接线：``ensure_registered()``（保证 spec 先入 registry）+
  ``register_handler``。``main.py::_startup`` 仅在 flag-on 时调用。

查找边界 = ``Path.cwd()``（项目根）：绝不越出项目读约定文件；
未命中返回 ``found=false`` 回执（工具层永不抛，与 ``read_skill`` 同款）。
"""

from __future__ import annotations

from agentix.agent.context.instructions import (
    DEFAULT_MAX_INSTRUCTION_CHARS,
    PROJECT_INSTRUCTION_FILES,
    find_project_instructions,
    read_instruction_text,
)

__all__ = ["install_read_agents_file_tool"]


def install_read_agents_file_tool() -> None:
    """注册 ``read_agents_file`` 处理器（幂等；依赖 flag-on 声明清单已有 spec）。"""
    from agentix.config.settings import settings

    if not bool(getattr(settings, "AGENTS_CASCADE_ENABLED", False)):
        return  # belt-and-suspenders：调用方已判 flag，这里再判一次
    from agentix.tools.gateway.executor import get_tool_executor
    from agentix.tools.registry_init import ensure_registered
    from agentix.tools.adapters.agents_file_read import (
        validate_read_agents_file_args,
    )

    ensure_registered()  # spec 先入 registry（executor 执行点要求 spec 存在）

    def _handler(args: dict) -> dict:
        # 工具层永不抛：非法入参/未命中 → 结构化回执（与 read_skill 同款）
        try:
            normalized = validate_read_agents_file_args(args)
        except ValueError as exc:
            return {"found": False, "reason": str(exc)}
        path = find_project_instructions(
            normalized["path"],
            names=PROJECT_INSTRUCTION_FILES,
            stop=_project_boundary(),
        )
        if path is None:
            return {
                "found": False,
                "reason": (
                    "no AGENTS.md/CLAUDE.md at or above "
                    f"{normalized['path']} within the project"
                ),
            }
        content = read_instruction_text(
            path, max_chars=DEFAULT_MAX_INSTRUCTION_CHARS
        )
        return {
            "found": True,
            "path": str(path),
            "content": content,
            "truncated": content.endswith("\n…[项目指令已截断]"),
        }

    get_tool_executor().register_handler("read_agents_file", _handler)


def _project_boundary():
    """项目边界（= cwd）：查找只在项目内走，绝不越界读文件。"""
    from pathlib import Path

    return Path.cwd()
