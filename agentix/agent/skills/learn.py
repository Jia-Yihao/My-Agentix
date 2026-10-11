"""Skill 草稿沉淀工具实现（P6-6.3；source-code-study §3.6 /learn 显式沉淀）。

把已验证的成功经验写成 ``data/skills_drafts/{name}/SKILL.md``——**隔离于**
``settings.SKILLS_DIR``：草稿不被 loader 扫描、不进目录前缀，人工评审把目录
搬进 ``skills/`` 晋升后才对模型生效（§5 表 6.3「Procedural Memory 沉淀
Skill」落点，与 P2 Skills 渐进披露交叉）。

校验纪律（工具层永不抛，返回 ``created=False + errors``）：

- 必填三键（name/description/anti_when，复用 ``loader.REQUIRED_FIELDS`` 语义）；
- 长度上限复用 ``loader`` 规范常量（description 120 / anti_when 240 / body 8000）；
- name 过 ``loader._NAME_RE``（无路径分隔符/控制字符——防穿越投毒式命名）；
- description/anti_when 单行（frontmatter 扁平 ``key: value`` 解析约束）；
- 同名草稿已存在 ⇒ 拒绝覆盖（**never overwrite**）；
- 写后 ``parse_skill_file`` 复验，失败即清理不留残目录。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from agentix.agent.skills.loader import (
    _NAME_RE,
    MAX_ANTI_WHEN_CHARS,
    MAX_DESCRIPTION_CHARS,
    MAX_SKILL_BODY_CHARS,
    parse_skill_file,
)

__all__ = ["DRAFT_DIR", "create_skill_draft"]

#: 草稿根目录（相对仓库根；与 SKILLS_DIR 隔离——模块级常量，测试可 monkeypatch）
DRAFT_DIR = "data/skills_drafts"


def create_skill_draft(
    args: Dict[str, Any], *, root: Optional[Path] = None
) -> Dict[str, Any]:
    """``create_skill_draft`` 工具语义：校验 → 写草稿 → 写后复验；从不抛。

    :param args: LLM 入参（name/description/anti_when/body）。
    :param root: 草稿根目录覆盖（测试注入 tmp_path；缺省 ``DRAFT_DIR``）。
    """
    draft_root = Path(root) if root is not None else Path(DRAFT_DIR)
    name = str(args.get("name", "") or "").strip()
    description = str(args.get("description", "") or "").strip()
    anti_when = str(args.get("anti_when", "") or "").strip()
    body = str(args.get("body", "") or "").strip()

    errors: List[str] = []
    if not name:
        errors.append("name is required")
    elif not _NAME_RE.match(name):
        errors.append(
            f"invalid skill name {name!r} (no path separators/control chars)"
        )
    if not description:
        errors.append("description is required (何时用——一句话)")
    elif len(description) > MAX_DESCRIPTION_CHARS:
        errors.append(
            f"description {len(description)} chars > {MAX_DESCRIPTION_CHARS}"
        )
    if not anti_when:
        errors.append("anti_when is required (何时不能用——负面描述纪律)")
    elif len(anti_when) > MAX_ANTI_WHEN_CHARS:
        errors.append(f"anti_when {len(anti_when)} chars > {MAX_ANTI_WHEN_CHARS}")
    if len(body) > MAX_SKILL_BODY_CHARS:
        errors.append(f"body {len(body)} chars > {MAX_SKILL_BODY_CHARS}")
    for label, value in (("description", description), ("anti_when", anti_when)):
        if "\n" in value or "\r" in value:
            errors.append(f"{label} must be a single line (frontmatter constraint)")
    if errors:
        return {"created": False, "errors": errors}

    target_dir = draft_root / name
    if target_dir.exists():
        return {
            "created": False,
            "error": f"draft {name!r} already exists (never overwrite)",
        }

    content = (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"anti_when: {anti_when}\n"
        "---\n\n"
        f"{body}\n"
    )
    try:
        target_dir.mkdir(parents=True, exist_ok=False)
        path = target_dir / "SKILL.md"
        path.write_text(content, encoding="utf-8")
        parse_skill_file(path, draft_root)  # 写后复验（frontmatter/必填/name 正则）
    except (OSError, ValueError) as exc:
        shutil.rmtree(target_dir, ignore_errors=True)
        return {"created": False, "errors": [f"draft validation failed: {exc}"]}
    return {
        "created": True,
        "name": name,
        "path": str(path),
        "draft_root": str(draft_root),
        "note": "draft only — outside skills/, promote manually after review",
    }
