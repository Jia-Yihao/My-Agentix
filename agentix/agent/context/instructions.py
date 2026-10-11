"""指令分层（Phase 18；Phase 21 增补 skills 段）：settings → skills → 项目 → 会话。

Phase 21（Skills 渐进披露）在 settings 层之后插入**元数据目录段**
（``settings.SKILLS_ENABLED`` gate，默认关 ⇒ 与 Phase 18 输出字节等价；
不 import skills 包、不扫磁盘）。详见 :func:`build_instructions`。

F1b2（M-P1-4 AGENTS 级联，``settings.AGENTS_CASCADE_ENABLED`` gate，
默认关 ⇒ 输出与基线逐字节一致）在既有三层之上追加两层：

- **全局层**：``~/.agentix/AGENTS.md``（常驻，与项目根同名约定）；
- **子目录层**：消息里**触达**的目录（:func:`mentioned_instruction_dirs`
  解析，存在且在项目边界内才收）向上找最近约定文件——触达才加载；
  两层均走内容 hash 去重 + 4k/12k 预算（既有机制复用，根级文件命中后
  子目录向上找到的同文件被去重直接丢弃）。运行期还可经
  ``read_agents_file`` 工具按需取回任意层级的约定文件（不重建 rules）。

借鉴 CLAUDE.md / AGENTS.md 约定（书 §2.9）：项目层按约定文件名从 cwd
向上逐级查找，命中即用；未命中返回空串（行为与既有完全一致，Strangler
零变化）。

约定文件名（按优先级，同目录先命中者胜）::

    AGENTS.md      # Codex/通用约定（仓库级）
    CLAUDE.md      # Claude Code 等价物

分层语义（顺序即注入顺序，先设置后会话）：

1. **settings 层**：调用方给定的系统规则（如 ``ContextEngine(system_rules=...)``）；
2. **skills 层**：SKILL.md 元数据目录（Phase 21）；
3. **全局层**（级联 flag-on）：``~/.agentix/`` 约定文件；
4. **项目层**：约定文件内容（向上查找，最多 ``max_chars``，超长确定性截断）；
5. **子目录层**（级联 flag-on）：消息触达目录的最近约定文件；
6. **会话层**：调用方给定的会话级指令（优先级最高，最后注入）。

渲染由各构建器负责（ContextEngine → ``RULE {block}`` 行；SimpleContextBuilder →
静态前缀块）；本模块只负责**发现、读取、排序**。

P3（岗位对照·渐进上下文，借鉴 claw-code 指令文件预算纪律）：

- **内容 hash 去重**：注入前按 sha256(块内容) 去重，保留首次出现
  （前缀稳定）——同一段规则不重复烧 token；
- **总预算**：全部块合计超 ``TOTAL_INSTRUCTION_CHARS``（常量，零新
  settings 键——延续本模块「零新键」决策）时，从**尾部（高优先级：
  会话层最后注入）**向前保配额，低优先级块先截后弃并附截断提示。
  生产输入（各层小块且不重复）两种机制均为 no-op ⇒ 输出与基线
  逐字节一致；单文件 4k 上限（``DEFAULT_MAX_INSTRUCTION_CHARS``）
  与总预算 12k 两级约束同 claw-code 的 4k/12k。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

__all__ = [
    "PROJECT_INSTRUCTION_FILES",
    "GLOBAL_INSTRUCTION_SUBDIR",
    "DEFAULT_MAX_INSTRUCTION_CHARS",
    "TOTAL_INSTRUCTION_CHARS",
    "find_project_instructions",
    "load_project_instructions",
    "read_instruction_text",
    "mentioned_instruction_dirs",
    "build_instructions",
]

#: 项目层约定文件名（按优先级）
PROJECT_INSTRUCTION_FILES: Tuple[str, ...] = ("AGENTS.md", "CLAUDE.md")

#: 全局层目录（级联 flag-on 时位于 ``Path.home()`` 之下；flag-off 不读取）
GLOBAL_INSTRUCTION_SUBDIR = ".agentix"

#: 项目层单文件读取上限（超长确定性截断；防止约定文件本身击穿上下文）
DEFAULT_MAX_INSTRUCTION_CHARS = 4000

#: P3：指令层**总预算**（settings+skills+项目+会话全块合计，借鉴 claw-code
#: 总预算 12k——单文件 4k 之外的第二道防线；常量决策，零新 settings 键）。
TOTAL_INSTRUCTION_CHARS = 12000

#: 总预算截断提示（与单文件截断提示同风格）
_TOTAL_TRUNC_MARKER = "\n…[指令块已截断]"


def find_project_instructions(
    start: Union[str, Path, None] = None,
    *,
    names: Sequence[str] = PROJECT_INSTRUCTION_FILES,
    stop: Union[str, Path, None] = None,
) -> Optional[Path]:
    """从 ``start``（默认 cwd）**向上逐级**查找约定文件；返回首个命中路径。

    - ``start`` 若指向文件，则从其父目录开始；
    - 同一目录按 ``names`` 优先级（AGENTS.md 先于 CLAUDE.md）；
    - ``stop``：向上查找的**最远目录**（含该层；测试 hermetic 边界用，
      ``None`` = 查到文件系统根——生产语义）；
    - 到达边界仍未命中 → ``None``。
    """
    cur = Path(start) if start is not None else Path.cwd()
    cur = cur.resolve()
    if cur.is_file():
        cur = cur.parent
    stop_path = Path(stop).resolve() if stop is not None else None
    for directory in (cur, *cur.parents):
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return candidate
        if stop_path is not None and directory == stop_path:
            break
    return None


def read_instruction_text(
    path: Union[str, Path],
    *,
    max_chars: int = DEFAULT_MAX_INSTRUCTION_CHARS,
    encoding: str = "utf-8",
) -> str:
    """读取约定文件并按 ``max_chars`` 确定性截断；读取失败一律 ``""``。

    超限截断附 ``\\n…[项目指令已截断]`` 提示（与项目层同一语义——约定文件
    本身也是内容，同样受 Budget 约束；L1 不静默）。
    """
    try:
        text = Path(path).read_text(encoding=encoding, errors="replace")
    except OSError:
        return ""
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars] + "\n…[项目指令已截断]"
    return text


def load_project_instructions(
    start: Union[str, Path, None] = None,
    *,
    names: Sequence[str] = PROJECT_INSTRUCTION_FILES,
    max_chars: int = DEFAULT_MAX_INSTRUCTION_CHARS,
    encoding: str = "utf-8",
    stop: Union[str, Path, None] = None,
) -> str:
    """读取项目层约定文件内容；未命中/读取失败一律 ``""``（工具层要稳，不抛）。

    超过 ``max_chars``（``>0`` 时）确定性截断并附截断提示——约定文件本身
    也是内容，同样受 Budget 约束（L1 不静默）。
    """
    path = find_project_instructions(start, names=names, stop=stop)
    if path is None:
        return ""
    return read_instruction_text(path, max_chars=max_chars, encoding=encoding)


def _content_hash(text: str) -> str:
    """块内容 sha256 前 16 hex——去重键（跨进程稳定，不依赖 PYTHONHASHSEED）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _dedupe_blocks(blocks: List[str]) -> List[str]:
    """按内容 hash 去重，**保留首次出现**（前缀稳定：settings 层永不后移）。"""
    seen = set()
    out: List[str] = []
    for block in blocks:
        key = _content_hash(block)
        if key in seen:
            continue
        seen.add(key)
        out.append(block)
    return out


def _fit_total_budget(blocks: List[str], budget: int = TOTAL_INSTRUCTION_CHARS) -> List[str]:
    """总预算裁剪：从**尾部（高优先级）**向前保配额，先截后弃（低优先级）。

    预算内（``sum <= budget``）原样返回（生产常态 ⇒ 与基线逐字节一致）。
    """
    if budget <= 0:
        return blocks
    # 含块间换行的合计长度（每块 +1，保守一个换行字符）
    total = sum(len(block) + 1 for block in blocks)
    if total <= budget:
        return blocks
    remaining = budget
    fitted: List[str] = []
    for block in reversed(blocks):
        if remaining <= 0:
            break
        if len(block) < remaining:
            fitted.append(block)
            remaining -= len(block) + 1
            continue
        # 本块放不下：截断并附提示，更低优先级的块整体放弃
        keep = max(0, remaining - len(_TOTAL_TRUNC_MARKER) - 1)
        fitted.append(block[:keep] + _TOTAL_TRUNC_MARKER)
        remaining = 0
    fitted.reverse()
    return fitted


#: 触达目录解析：像路径的 token（含至少一个分隔符的类路径串）
_DIR_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./\\~-]{2,118}")

#: 单条消息最多解析的触达目录数（有界——级联不能被一条消息打爆）
_MAX_MENTIONED_DIRS = 8


def mentioned_instruction_dirs(
    message: str,
    *,
    root: Union[str, Path, None] = None,
    limit: int = _MAX_MENTIONED_DIRS,
) -> Tuple[str, ...]:
    """从消息文本解析**触达目录**（M-P1-4「子目录按需加载」的入口）。

    只收同时满足的候选：像路径（含 ``/`` 或 ``\\``）、去掉文件名后是
    **存在的目录**、且解析后仍在 ``root``（默认 cwd，即项目根）边界内。
    顺序 = 出现顺序（确定性），同目录去重保首次，最多 ``limit`` 个。

    - **flag-off（默认）立即返回空元组**——级联关时零解析零 IO；
    - 绝不越出项目根（``root`` 排除项：逃逸路径、裸词、绝对路径拼接失败）。
    """
    if not message:
        return ()
    from agentix.config.settings import settings

    if not bool(getattr(settings, "AGENTS_CASCADE_ENABLED", False)):
        return ()
    base = Path(root) if root is not None else Path.cwd()
    try:
        base = base.resolve()
    except OSError:
        return ()
    out: List[str] = []
    seen = set()
    for match in _DIR_TOKEN_RE.finditer(message):
        raw = match.group(0)
        if "/" not in raw and "\\" not in raw:
            continue  # 裸词不是路径
        token = raw.rstrip(".")  # 句尾句点归一（保留尾部分隔符 = 目录提及）
        parent = token.replace("\\", "/").rsplit("/", 1)[0]
        while parent.startswith("./"):
            parent = parent[2:]
        if not parent or parent in seen:
            continue
        try:
            resolved = (base / parent).resolve()
        except OSError:
            continue
        if resolved != base and base not in resolved.parents:
            continue  # 逃逸项目根，丢弃
        if not resolved.is_dir():
            continue
        seen.add(parent)
        out.append(parent)
        if len(out) >= max(1, limit):
            break
    return tuple(out)


def build_instructions(
    *,
    settings_rules: Sequence[str] = (),
    project_start: Union[str, Path, None] = None,
    load_project: bool = True,
    session_rules: Sequence[str] = (),
    names: Sequence[str] = PROJECT_INSTRUCTION_FILES,
    max_chars: int = DEFAULT_MAX_INSTRUCTION_CHARS,
    stop: Union[str, Path, None] = None,
    skills_enabled: Optional[bool] = None,
    global_start: Union[str, Path, None] = None,
    subdirs: Sequence[Union[str, Path]] = (),
    cascade_enabled: Optional[bool] = None,
) -> Tuple[str, ...]:
    """多层合并：settings → skills → 全局 → 项目 → 子目录 → 会话；返回非空块的有序元组。

    - 空白块不注入（任一层为空即跳过，不产生多余空行）；
    - ``load_project=False`` 跳过项目层（测试/hermetic 用）；
    - **Skills 段（Phase 21）**：``skills_enabled is None`` 时读
      ``settings.SKILLS_ENABLED``；flag-off（默认）时本层完全不触发——
      不 import skills 包、不扫磁盘，输出与 Phase 18 **字节等价**。
      flag-on 时注入元数据目录（name/when/anti_when），正文仍由
      ``read_skill`` 惰性拉取（两段式：目录进前缀，正文只进轨迹）。
    - **级联段（F1b2·M-P1-4，``cascade_enabled is None`` 时读
      ``settings.AGENTS_CASCADE_ENABLED``，默认关 ⇒ 以下两层零触发、
      输出与基线逐字节一致）**：
      全局层 = ``global_start``（默认 ``~/.agentix/``）下约定文件（常驻）；
      子目录层 = ``subdirs``（调用方给的触达目录，通常来自
      :func:`mentioned_instruction_dirs`）各自向上找最近约定文件——
      触达才加载，向上命中根级文件时被 hash 去重直接丢弃。
    - **P3 预算纪律**：注入前内容 hash 去重（保首次出现 ⇒ 前缀稳定），再按总预算从尾部
      高优先级向前裁剪；不重复、合计预算内的输入两种机制均 no-op ——
      生产输出与既有行为逐字节一致。
    """
    if skills_enabled is None or cascade_enabled is None:
        from agentix.config.settings import settings

        if skills_enabled is None:
            skills_enabled = bool(settings.SKILLS_ENABLED)
        if cascade_enabled is None:
            cascade_enabled = bool(getattr(settings, "AGENTS_CASCADE_ENABLED", False))
    blocks: List[str] = [str(rule) for rule in settings_rules if str(rule).strip()]
    if skills_enabled:
        from agentix.agent.skills.registry import catalog_block

        catalog = catalog_block()
        if catalog.strip():
            blocks.append(catalog)
    if cascade_enabled:
        # 全局层（常驻，flag-on 才读；读失败按既有语义给 ""）
        global_dir = (
            Path(global_start) if global_start is not None else Path.home() / GLOBAL_INSTRUCTION_SUBDIR
        )
        for name in names:
            candidate = global_dir / name
            if candidate.is_file():
                text = read_instruction_text(candidate, max_chars=max_chars)
                if text.strip():
                    blocks.append(text)
                break
    if load_project:
        project = load_project_instructions(
            project_start, names=names, max_chars=max_chars, stop=stop
        )
        if project.strip():
            blocks.append(project)
    if cascade_enabled and subdirs:
        # 子目录层（触达才加载）：各自向上找最近约定文件；边界沿 stop /
        # project_start，向上命中已注入的根级文件时由 hash 去重丢弃。
        boundary = stop if stop is not None else project_start
        for subdir in subdirs:
            text = load_project_instructions(
                subdir, names=names, max_chars=max_chars, stop=boundary
            )
            if text.strip():
                blocks.append(text)
    blocks.extend(str(rule) for rule in session_rules if str(rule).strip())
    # P3：先按内容 hash 去重（保首次出现 ⇒ 前缀稳定），再按总预算从尾部
    # 高优先级向前裁剪；两者对不重复、合计 ≤12k 的输入均为 no-op ——
    # 生产输出与既有行为逐字节一致。
    deduped = _dedupe_blocks(blocks)
    return tuple(_fit_total_budget(deduped))
