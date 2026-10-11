"""P40：全仓模块可导入性守卫（import health）。

**为什么单独建这个文件**：批次 13 在两处发现**断链 import**——
模块引用了**根本不存在**的目标，导致整个文件（甚至整个家族）**永久无法导入**：

| 文件 | 断链 import | 后果 |
|---|---|---|
| `ps_genai_agents/components/guardrails/prompts.py` | `from ..utils.utils import ...`（无 `utils` 包） | 整个 `ps_genai_agents` 家族不可导入 |
| `application/services/search_service.py` | `from agentix.tools import ...`（无 `agentix.tools`） | 该文件 99 语句**永远 0% 覆盖**（文件已按死代码两步流程删除——Phase 27，留有防复活守卫） |

两处都是**静默失败**：没有测试引用它们，所以没人发现。
而"永远无法导入"的模块还有个隐蔽危害——它们**拉低覆盖率分母却永远不可能被覆盖**，
让覆盖率数字失真（这正是 S6 覆盖率门禁迟迟不达标的原因之一）。

本文件遍历 `agentix.*` 的全部子模块并逐个导入，
任何新的断链 import 都会**立刻**在这里失败。

**已知豁免**：确属历史遗留且无法修复的模块登记在 `KNOWN_BROKEN` 中，
每条必须写明原因与后续处置（新增豁免须在 PR 中说明理由）。
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import List, Tuple

import pytest

import agentix

#: **代码缺陷**导致的不可导入模块（模块名后缀 → 原因）。新增必须写明理由与后续处置。
#:
#: 2026-09：本表最后一条 ``text2cypher_with_viz_and_follow_ups`` 已随授权清理
#: 一并**删除源文件**（该文件 ``from .edges import viz_mapper_edge`` 而 ``edges.py``
#: 无此符号 ⇒ 永久无法导入，唯一作用是让本表长期非空）。**现应为空**：
#: 代码缺陷豁免表不是"允许清单"，空表才是健康状态。
KNOWN_BROKEN: dict = {}

#: **环境缺陷**（缺可选第三方依赖）导致的不可导入——不是代码 bug，单独归类。
#: 这些包未列入 requirements.txt，属"按需安装"的可选依赖。
#: 现应为空：原 `retrievers.cypher_examples.vector_store`（neo4j_graphrag）与
#: `ui.components`（streamlit）两条随 A6 死代码批删文件后清空（2026-10-02）。
KNOWN_MISSING_DEPS: dict = {}


def _iter_module_names():
    yield from sorted(
        m.name for m in pkgutil.walk_packages(agentix.__path__, agentix.__name__ + ".")
    )


def _is_exempt(name: str) -> bool:
    return any(name.endswith(s) for s in KNOWN_BROKEN) or any(
        name.endswith(s) for s in KNOWN_MISSING_DEPS
    )


def _import_all() -> List[Tuple[str, str]]:
    """返回 [(模块名, 错误摘要)]；空列表表示全部可导入（豁免项已排除）。"""
    broken: List[Tuple[str, str]] = []
    for name in _iter_module_names():
        if _is_exempt(name):
            continue
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - 正是要收集失败
            broken.append((name, f"{type(exc).__name__}: {exc}"))
    return broken


class TestImportHealth:
    def test_no_broken_internal_imports(self):
        """**代码层面**不得有断链 import（目标模块/符号不存在）。

        与"缺可选第三方依赖"区分开：后者是环境问题（见 KNOWN_MISSING_DEPS），
        前者是 bug —— 它让文件永远不可能被覆盖，还会污染覆盖率分母。
        """
        broken = _import_all()
        assert not broken, (
            "以下模块存在断链 import（目标不存在 / 导入期报错）。"
            "它们既无法被测试覆盖，也会污染覆盖率分母：\n"
            + "\n".join(f"  {name}\n      {err}" for name, err in broken)
            + "\n\n修复 import；若确属遗留死代码，在 KNOWN_BROKEN 中登记理由"
            "（并在源码加 DEAD-CODE(P2-5) 标注）。"
        )

    def test_known_lists_are_documented(self):
        """豁免项必须附理由，且**条目不得随意增长**（否则守卫退化）。"""
        for suffix, reason in list(KNOWN_BROKEN.items()) + list(
            KNOWN_MISSING_DEPS.items()
        ):
            assert reason, f"豁免项 {suffix} 未写明理由"
        assert len(KNOWN_BROKEN) <= 3, "代码缺陷豁免过多，请优先修复而非豁免"

    def test_known_broken_sources_are_annotated(self):
        """登记为死代码的模块，源码里必须真的有 DEAD-CODE 标注（防登记了事）。"""
        from pathlib import Path

        src_root = Path(agentix.__file__).resolve().parent
        for suffix in KNOWN_BROKEN:
            matches = [
                p
                for p in src_root.rglob(f"*{suffix}.py")
                if "DEAD-CODE" in p.read_text(encoding="utf-8")
            ]
            assert matches, f"{suffix} 未在源码中标注 DEAD-CODE"

    def test_code_defect_exemption_list_is_empty(self):
        """★ 代码缺陷豁免表应当为空——豁免项存在本身就是一个待办。

        与 ``len(KNOWN_BROKEN) <= 3`` 的上限断言互补：上限防"越加越多"，
        本条防"修完忘了删"。一个长期非空的豁免表会把"有 N 个文件永远无法导入"
        正常化，而这件事本不该被正常化。
        """
        assert KNOWN_BROKEN == {}, (
            f"代码缺陷豁免表非空：{sorted(KNOWN_BROKEN)}。"
            "要么修掉断链 import，要么走授权流程删除文件，而不是长期登记。"
        )

    def test_deleted_legacy_workflow_does_not_reappear(self):
        """回归守卫：已删除的不可导入遗留工作流不得复活。

        删除依据（2026-09，已授权）：该文件全仓 0 处 import，且
        ``from .edges import viz_mapper_edge`` 引用不存在的符号 ⇒ **永久无法导入**。
        它存在一天，``KNOWN_BROKEN`` 就必须非空一天。
        回滚点：git ``dbca16d``。
        """
        from pathlib import Path

        src_root = Path(agentix.__file__).resolve().parent
        resurrected = list(src_root.rglob("text2cypher_with_viz_and_follow_ups.py"))
        assert not resurrected, (
            f"已删除的遗留工作流又出现了：{[str(p) for p in resurrected]}。"
            "如确需恢复，请先修好它的断链 import 并同步本测试与 KNOWN_BROKEN 说明。"
        )

    def test_previously_broken_modules_now_import(self):
        """回归守卫：批次 13 修好的模块必须保持可导入。

        ``search_service`` 曾在本守卫内（断链 import 已修），后按
        ``evolution-assessment.md`` §4 死代码两步流程**整体删除**
        （零调用面 + 修复后仍 0 覆盖），现由下面的防复活守卫覆盖。
        """
        importlib.import_module(
            "agentix.application.agents.kg_sub_graph.ps_genai_agents.components.guardrails.prompts"
        )

    def test_deleted_search_service_stays_deleted(self):
        """防复活：已删除的 ``search_service`` / ``search_prompts`` 不得再出现。

        与 ``text2cypher_with_viz_and_follow_ups`` 同口径：删除是**审计结论**
        （全仓零调用、5 类事件零引用、整类无 importer），恢复前必须先给出
        调用方并同步本守卫与 evolution-assessment §4。
        """
        from pathlib import Path

        src_root = Path(agentix.__file__).resolve().parent
        resurrected = [
            p.name
            for name in (
                "search_service.py",
                "search_prompts.py",
            )
            for p in src_root.rglob(name)
        ]
        assert not resurrected, f"已删除的死代码又出现了：{resurrected}"

    def test_agentix_tools_import_targets_exist(self):
        """AST 静态检查：`agentix.tools*` 的 import 目标模块必须真实存在。

        历史注记：本仓曾长期**禁止**引用 `agentix.tools`（它不存在，
        `search_service.py` 的修复说明里留过旧语句）。Stage M·M1（2026-10-02）
        已创建 `agentix/tools/`（gateway/registry/adapters）为
        `capabilities/tools` 的规范位——守卫由「禁止引用」翻转为「引用必须
        指向真实文件」（防搬家后 import 断链）。
        用 AST 而非字符串匹配：字符串匹配会命中注释/文档里的说明文字。
        """
        import ast
        from pathlib import Path

        src_root = Path(agentix.__file__).resolve().parent
        offenders: List[str] = []
        for path in src_root.rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, OSError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                targets: List[str] = []
                if isinstance(node, ast.Import):
                    targets = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    targets = [node.module]
                for target in targets:
                    if target == "agentix.tools" or target.startswith("agentix.tools."):
                        rel = Path(*target.split("."))
                        if not (
                            (src_root.parent / rel).is_dir()
                            or (src_root.parent / rel).with_suffix(".py").is_file()
                        ):
                            offenders.append(
                                f"{path.relative_to(src_root.parent)}:{node.lineno} -> {target}"
                            )
        assert not offenders, f"agentix.tools import 指向不存在的模块：{offenders}"


@pytest.mark.slow
class TestImportSideEffects:
    def test_importing_domain_does_not_drag_upstream(self):
        """与 test_p17 同口径，但针对**全模块**抽样：domain 子包不得拉起上层。"""
        import subprocess
        import sys

        code = (
            "import sys\n"
            "import agentix.domain.errors\n"
            "import agentix.domain.ports\n"
            "import agentix.domain.policies\n"
            "print(sum(1 for m in sys.modules "
            "if m.startswith(('agentix.infrastructure', 'agentix.interfaces'))))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=180
        )
        assert result.stdout.strip() == "0", f"domain 导入链污染上层：{result.stdout}"
