"""Skills 渐进披露（Phase 21，书 §2.6 / §4.7）：两段式加载。

第一段（静态元数据目录）：:mod:`agentix.agent.skills.loader` 扫描
``settings.SKILLS_DIR`` 下 ``SKILL.md``，只取 name/description/anti_when，
经 ``build_instructions`` flag-on 注入静态前缀——**正文永不进前缀**。

第二段（惰性正文）：LLM 按目录用 ``read_skill`` 工具拉正文进轨迹，
路径只来自扫描结果并做 ``relative_to(root)`` 纵深校验（防穿越）。
"""

from __future__ import annotations

from agentix.agent.skills.loader import SkillMeta, parse_skill_file, scan_skills
from agentix.agent.skills.registry import (
    catalog_block,
    catalog_lines,
    get_registry,
    read_skill,
    reset_registry,
    skill_names,
)

__all__ = [
    "SkillMeta",
    "parse_skill_file",
    "scan_skills",
    "catalog_block",
    "catalog_lines",
    "get_registry",
    "read_skill",
    "reset_registry",
    "skill_names",
    "install_read_skill_tool",
]


def install_read_skill_tool() -> None:
    """惰性转出（避免 ``__init__`` import 期碰 capabilities）。"""
    from agentix.agent.skills.install import install_read_skill_tool as _install

    _install()
