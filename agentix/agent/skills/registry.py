"""Skill 注册表：进程级懒加载单例 + ``read_skill`` 惰性正文读取（第二段）。

分层：本模块属 ``agentix.agent``，可 import ``agentix.capabilities``（向上）；
capabilities 永不反向 import 本包。

两段式
- **静态段**：``catalog_block()`` 返回元数据目录（供 ``build_instructions``
  flag-on 注入，flag-off 字节不变）；
- **轨迹段**：``read_skill(name)`` 按注册表查 name → 磁盘读正文，
  **name 不参与路径拼接**（路径只来自扫描时存下的 ``SkillMeta.path``），
  read 时再做一次 ``relative_to(root)`` 纵深校验（p43 风格防穿越）。
"""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional, Tuple

from loguru import logger

from agentix.agent.skills.loader import (
    MAX_SKILL_BODY_CHARS,
    SkillMeta,
    scan_fingerprint,
    scan_skills,
)

__all__ = [
    "get_registry",
    "reset_registry",
    "catalog_block",
    "catalog_lines",
    "skill_names",
    "read_skill",
    "SkillRegistry",
]


class SkillRegistry:
    """name → SkillMeta 的只读注册表（first-wins 由 loader 保证）。"""

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()
        self._skills: Dict[str, SkillMeta] = {}
        for meta in scan_skills(self._root):
            self._skills.setdefault(meta.name, meta)
        # P2 快照指纹：与扫描同批取（stat 级，不读内容）。
        self._fingerprint = scan_fingerprint(self._root)
        self._refresh_lock = Lock()

    @property
    def root(self) -> Path:
        return self._root

    def maybe_refresh(self) -> bool:
        """清单指纹变化才重扫（mtime/size 快照失效；未变 ⇒ 零重扫）。

        借鉴 hermes 技能 manifest 快照缓存（prompt_builder 快照比对 +
        skill_commands 热重载）：目录增/删/改**无需 ``reset_registry``**
        即可被下一次 ``get_registry()`` 访问看见；扫描/解析/校验只在指纹
        变化时发生。重建完成后**原子替换** ``_skills``（读者要么拿到旧
        快照要么拿到新快照，不会看到半更新状态）。

        :returns: 是否发生了重扫。
        """
        if scan_fingerprint(self._root) == self._fingerprint:
            return False
        with self._refresh_lock:
            if scan_fingerprint(self._root) == self._fingerprint:
                return False  # 双检：进锁前别的线程已刷新
            fresh: Dict[str, SkillMeta] = {}
            for meta in scan_skills(self._root):
                fresh.setdefault(meta.name, meta)
            self._skills = fresh  # 原子替换
            # 指纹在读取**之后**取：扫描期间若又被改动，下次访问会再触发
            # 一次刷新收敛（宁可多刷一次，不留陈旧指纹）。
            self._fingerprint = scan_fingerprint(self._root)
            logger.info("skill catalog refreshed: {} skill(s)", len(fresh))
            return True

    def names(self) -> Tuple[str, ...]:
        return tuple(self._skills)

    def get(self, name: str) -> Optional[SkillMeta]:
        return self._skills.get(name)

    def catalog_lines(self) -> Tuple[str, ...]:
        return tuple(m.catalog_line() for m in self._skills.values())

    def read_body(self, name: str) -> str:
        """读正文（去 frontmatter、截断）；查不到/越界抛 ``KeyError``。"""
        meta = self._skills.get(name)
        if meta is None:
            raise KeyError(name)
        path = meta.path.resolve()
        try:
            path.relative_to(self._root)
        except ValueError:  # pragma: no cover - 扫描期已保证，纵深防御
            logger.warning("skill path escapes root, refused: {}", path)
            raise KeyError(name) from None
        text = path.read_text(encoding="utf-8", errors="replace")
        body = _strip_frontmatter(text)
        if len(body) > MAX_SKILL_BODY_CHARS:
            body = body[:MAX_SKILL_BODY_CHARS] + "\n\n[skill body truncated]"
        return body


def _strip_frontmatter(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text.strip()
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1 :]).strip()
    return text.strip()


_lock = Lock()
_registry: Optional[SkillRegistry] = None


def get_registry() -> SkillRegistry:
    """懒加载单例（首次访问时扫描磁盘）；``SKILLS_DIR`` 变更见 ``reset_registry``。

    每次访问附带 ``maybe_refresh()``（stat 级指纹检查，未变零重扫）——
    目录热改无需手动 reset；正文本就每次 ``read_body`` 现读磁盘。
    """
    global _registry
    registry = _registry
    if registry is None:
        with _lock:
            registry = _registry
            if registry is None:
                from agentix.config.settings import settings

                root = Path(settings.SKILLS_DIR)
                registry = SkillRegistry(root)
                _registry = registry
    registry.maybe_refresh()
    return registry


def reset_registry() -> None:
    """清空单例（测试与目录变更后重扫用）。"""
    global _registry
    with _lock:
        _registry = None


def skill_names() -> Tuple[str, ...]:
    return get_registry().names()


def catalog_lines() -> Tuple[str, ...]:
    return get_registry().catalog_lines()


def catalog_block() -> str:
    """元数据目录整块（含引导语）；无条目返回 ``""``。

    只含 name/description/anti_when——正文永远不进前缀。
    """
    lines = catalog_lines()
    if not lines:
        return ""
    head = "Skills（目录，按需用 read_skill 工具读取正文）："
    return "\n".join([head, *lines])


def read_skill(name: str) -> Dict[str, object]:
    """``read_skill`` 工具语义：未知名字不抛，返回友好回执 + 可用清单。"""
    registry = get_registry()
    try:
        body = registry.read_body(str(name))
    except KeyError:
        names: List[str] = list(registry.names())
        return {
            "found": False,
            "error": f"unknown skill: {name!r}",
            "available_skills": names,
        }
    return {"found": True, "name": name, "body": body}
