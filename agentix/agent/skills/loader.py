"""Skills 渐进披露（Phase 21，书 §2.6 / §4.7）第一层：SKILL.md 发现与校验。

两段式加载的**第一段**——扫描根目录下全部 ``SKILL.md``，只抽取**元数据**
（name / description / anti_when），正文留在磁盘，由
:mod:`agentix.agent.skills.registry` 经 ``read_skill`` 惰性读入轨迹。

frontmatter 格式（扁平 ``key: value``，不依赖 YAML 库）::

    ---
    name: legal-citation
    description: 需要「数据集 + 条号」精确引用格式输出时
    anti_when: 语义检索/开放问答（走 graphrag），或只需条文大意时
    ---
    正文 Markdown……

校验纪律（书 §4.3 负面描述原则）：

- ``name`` / ``description`` / ``anti_when`` 三键**必填**——缺 ``anti_when``
  即「只写能做什么、不写不能做什么」，本阶段直接**跳过该条目并告警**；
- ``name`` 不允许控制字符与路径分隔符（防投毒式命名 + 保证查表语义，
  正文路径永远来自扫描结果、不从 name 拼接）；
- 任一文件解析失败一律告警跳过——扫描器绝不抛异常打断启动。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

from loguru import logger

__all__ = [
    "SkillMeta",
    "REQUIRED_FIELDS",
    "MAX_SKILL_BODY_CHARS",
    "MAX_DESCRIPTION_CHARS",
    "MAX_ANTI_WHEN_CHARS",
    "parse_skill_file",
    "scan_skills",
    "scan_fingerprint",
    "skill_standards_errors",
]

#: frontmatter 必填键（缺一即整条跳过）
REQUIRED_FIELDS: Tuple[str, ...] = ("name", "description", "anti_when")

#: name 合法字符：字母/数字/下划线/连字符/点/中文（无路径分隔符、无控制字符）
_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f/\\:*?\"<>|]+$")

#: 正文读取上限（与项目指令层同尺度；超长截断不静默——附截断提示）
MAX_SKILL_BODY_CHARS = 8000

#: P2 作者规范上限（**规范即测试**，hermes tests/skills/test_authoring_standards
#: 思路）：description/anti_when 都要进目录行**常驻前缀**——一句话封顶，
#: 超长即前缀膨胀（由 shipped-skill 测试强制，扫描器本身不因此拒收）。
MAX_DESCRIPTION_CHARS = 120
MAX_ANTI_WHEN_CHARS = 240


@dataclass(frozen=True)
class SkillMeta:
    """一条 Skill 的元数据（不含正文）。"""

    name: str
    description: str  # when_to_use：何时用（一句话）
    anti_when: str  # 何时**不能**用（负面条件，必填）
    path: Path  # SKILL.md 绝对路径（来自扫描，不从 name 拼接）
    root: Path  # 所属扫描根（read 时做 relative_to 纵深校验）

    def catalog_line(self) -> str:
        """注入静态前缀的单行目录条目（元数据，绝不含正文）。"""
        return f"- {self.name}：{self.description}（不适用：{self.anti_when}）"


def _parse_frontmatter(text: str, path: Path) -> Tuple[dict, str]:
    """拆 frontmatter 与正文；格式不合法抛 ``ValueError``。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{path}: missing opening '---'")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        raise ValueError(f"{path}: missing closing '---'")
    meta: dict = {}
    for raw in lines[1:end]:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"{path}: bad frontmatter line {line!r}")
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    body = "\n".join(lines[end + 1 :]).strip()
    return meta, body


def parse_skill_file(path: Path, root: Path) -> SkillMeta:
    """解析单个 SKILL.md → ``SkillMeta``；任何校验失败抛 ``ValueError``。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    meta, _body = _parse_frontmatter(text, path)
    missing = [k for k in REQUIRED_FIELDS if not str(meta.get(k, "")).strip()]
    if missing:
        raise ValueError(f"{path}: missing required fields {missing}")
    name = str(meta["name"])
    if not _NAME_RE.match(name):
        raise ValueError(f"{path}: invalid skill name {name!r}")
    return SkillMeta(
        name=name,
        description=str(meta["description"]),
        anti_when=str(meta["anti_when"]),
        path=path.resolve(),
        root=root.resolve(),
    )


def scan_skills(root: Path) -> Tuple[SkillMeta, ...]:
    """递归扫描 ``root`` 下全部 ``SKILL.md``；失败条目告警跳过，绝不抛。

    - 名字重复：**先到先得**（按路径排序保证确定性），后来者告警跳过；
    - 根目录不存在：返回空元组（默认关/未配置目录 = 合法现状）。
    """
    root = Path(root)
    if not root.is_dir():
        return ()
    seen: List[SkillMeta] = []
    names: set = set()
    for path in sorted(root.rglob("SKILL.md")):
        if not path.is_file():
            continue
        try:
            meta = parse_skill_file(path, root)
        except ValueError as exc:
            logger.warning("skill skipped (validation failed): {}", exc)
            continue
        except OSError as exc:
            logger.warning("skill skipped (unreadable): {} — {}", path, exc)
            continue
        if meta.name in names:
            logger.warning("skill skipped (duplicate name {!r}): {}", meta.name, path)
            continue
        names.add(meta.name)
        seen.append(meta)
    return tuple(seen)


def scan_fingerprint(root: Path) -> Tuple[Tuple[str, int, int], ...]:
    """清单指纹：``(相对路径, mtime_ns, size)`` 升序元组——**只 stat 不读不解析**。

    ``SkillRegistry.maybe_refresh`` 据此做快照失效（hermes「缓存失效显式化」
    纪律：每轮只付 stat 成本，扫描/解析/校验只在指纹变化时发生）。
    根不存在 ⇒ 空元组；单文件 stat 失败 ⇒ 跳过（下次指纹自然不匹配）。
    """
    root = Path(root)
    if not root.is_dir():
        return ()
    entries: List[Tuple[str, int, int]] = []
    for path in root.rglob("SKILL.md"):
        try:
            st = path.stat()
            rel = path.relative_to(root).as_posix()  # posix 分隔符：跨平台指纹可比
        except OSError:
            continue
        entries.append((rel, st.st_mtime_ns, st.st_size))
    return tuple(sorted(entries))


def skill_standards_errors(meta: SkillMeta, *, body_chars: int = 0) -> List[str]:
    """作者规范校验（**只列问题不抛**）——shipped-skill 测试与写作自查共用。

    必填三键由扫描期兜底（缺项整条跳过）；这里管的是规范层：
    长度上限（目录行常驻前缀不膨胀）、正文不被截断、name 与目录一致
    （查表语义 + 防「改名忘改目录」的双真源）。
    """
    errors: List[str] = []
    if len(meta.description) > MAX_DESCRIPTION_CHARS:
        errors.append(
            f"description {len(meta.description)} chars > {MAX_DESCRIPTION_CHARS}"
        )
    if len(meta.anti_when) > MAX_ANTI_WHEN_CHARS:
        errors.append(f"anti_when {len(meta.anti_when)} chars > {MAX_ANTI_WHEN_CHARS}")
    if body_chars > MAX_SKILL_BODY_CHARS:
        errors.append(
            f"body {body_chars} chars > {MAX_SKILL_BODY_CHARS} (read_skill would truncate)"
        )
    if meta.path.parent.name != meta.name:
        errors.append(
            f"name {meta.name!r} != directory {meta.path.parent.name!r}"
        )
    return errors
