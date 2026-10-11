"""P36：代码结构卫生守卫（批次 13「代码结构整理」的产物把关）。

Background：结构扫描时发现两类**真实**隐患，本文件把它们钉死，防止复发：

1. **同名常量、不同内容**：`GUARDRAILS_SYSTEM_PROMPT` 曾在 `lg_prompts.py`
   与 `kg_prompts.py` **同名定义但内容不同**（主图应用范围检查 vs KG 子图
   图谱范围判定）。同名极易误用——改一处以为改了两处。
   已新增语义明确的正式名（`GUARDRAILS_APP_SCOPE_*` / `GUARDRAILS_GRAPH_SCOPE_*`），
   旧名保留为别名；本测试确保**新出现的**同名异值常量会被立即发现。

2. **同语义文案分裂**：`NO_CYPHER_RESULTS` 曾有一份英文（模块级）与一份中文
   （函数内局部），同一"无结果"语义两套文案。已统一到 `constants.py` 单一来源。

设计原则：**AST 扫描 + 白名单**，不 import 业务模块（避免副作用与导入开销）。
白名单用于登记「已知且已加注释说明」的历史项，新增项必须显式登记并说明理由。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "agentix"

#: 已知的同名异值常量（已加澄清注释 + 语义化别名），新增需在此登记并说明理由
#: 注：`_TARGET`（Phase 6 Strangler shim 模板常量）已随 Stage M·M6（2026-10-03）
#: 三簇 shim 全数删除而从代码库消失——p75 断言零残留，不再需要白名单条目。
KNOWN_DUPLICATE_CONSTANTS: Dict[str, str] = {
    "GUARDRAILS_SYSTEM_PROMPT": (
        "主图应用范围检查（lg_prompts）与 KG 子图图谱范围判定（kg_prompts）同名；"
        "两者**本就不同**，已各自新增语义化正式名，旧名仅作兼容别名"
    ),
}


def _module_level_string_constants(path: Path) -> Dict[str, str]:
    """提取模块级「常量名 → 字符串字面量」（仅 str 常量，够用且稳）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return {}

    out: Dict[str, str] = {}
    for node in tree.body:  # 只看模块级
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name) or not target.id.isupper():
                continue
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                out[target.id] = node.value.value
            elif isinstance(node.value, ast.Name):
                # 形如 `A = B`（兼容别名）——记录被别名指向的名字，便于查证
                out[target.id] = f"<alias-of:{node.value.id}>"
    return out


def _scan_duplicates() -> Dict[str, List[str]]:
    """返回 {常量名: [定义它的文件, ...]}，只保留**同名且异值**的。"""
    seen: Dict[str, Dict[str, str]] = {}  # name -> {file: value}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        for name, value in _module_level_string_constants(path).items():
            seen.setdefault(name, {})[str(path.relative_to(PROJECT_ROOT))] = value

    dup: Dict[str, List[str]] = {}
    for name, by_file in seen.items():
        if len(by_file) < 2:
            continue
        distinct = {v for v in by_file.values() if not v.startswith("<alias-of:")}
        if len(distinct) > 1:  # 同名且确实不同值
            dup[name] = sorted(by_file)
    return dup


# ---------------------------------------------------------------------------
# 1. 同名异值常量
# ---------------------------------------------------------------------------


class TestNoAmbiguousDuplicateConstants:
    def test_no_unregistered_duplicate_constants(self):
        duplicates = _scan_duplicates()
        unregistered = {k: v for k, v in duplicates.items()
                        if k not in KNOWN_DUPLICATE_CONSTANTS}
        assert not unregistered, (
            "发现同名但内容不同的模块级常量（易误用，改一处以为改了两处）：\n"
            + "\n".join(f"  {k}: {v}" for k, v in unregistered.items())
            + "\n请重命名为语义明确的正式名（旧名保留别名），或在本测试白名单中登记理由。"
        )

    def test_known_duplicates_are_disambiguated(self):
        """已登记项必须**真的**有语义化正式名（不能只是登记了事）。"""
        for name in KNOWN_DUPLICATE_CONSTANTS:
            if name != "GUARDRAILS_SYSTEM_PROMPT":
                continue
            lg = SOURCE_ROOT / "application" / "agents" / "lg_prompts.py"
            kg = (
                SOURCE_ROOT
                / "application"
                / "agents"
                / "kg_sub_graph"
                / "prompts"
                / "kg_prompts.py"
            )
            lg_src = lg.read_text(encoding="utf-8")
            kg_src = kg.read_text(encoding="utf-8")
            assert "GUARDRAILS_APP_SCOPE_SYSTEM_PROMPT" in lg_src
            assert "GUARDRAILS_GRAPH_SCOPE_SYSTEM_PROMPT" in kg_src
            # 两处都必须写明「同名易误用」
            assert "同名" in lg_src and "同名" in kg_src

    def test_aliases_point_to_the_new_names(self):
        """兼容别名必须指向正式名（否则等于又抄了一份）。"""
        import importlib

        lg = importlib.import_module("agentix.application.agents.lg_prompts")
        kg = importlib.import_module(
            "agentix.application.agents.kg_sub_graph.prompts.kg_prompts"
        )
        assert lg.GUARDRAILS_SYSTEM_PROMPT is lg.GUARDRAILS_APP_SCOPE_SYSTEM_PROMPT
        assert kg.GUARDRAILS_SYSTEM_PROMPT is kg.GUARDRAILS_GRAPH_SCOPE_SYSTEM_PROMPT
        assert lg.GUARDRAILS_SYSTEM_PROMPT != kg.GUARDRAILS_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# 2. 同语义文案单一来源
# ---------------------------------------------------------------------------


class TestSingleSourceMessages:
    def test_no_cypher_results_defined_once(self):
        """`NO_CYPHER_RESULTS` 只能在 constants.py 定义一次（不得再内联）。"""
        constants = (
            SOURCE_ROOT
            / "application"
            / "agents"
            / "kg_sub_graph"
            / "agentic_rag_agents"
            / "constants.py"
        )
        assert constants.is_file()
        assert "NO_CYPHER_RESULTS" in constants.read_text(encoding="utf-8")

        offenders: List[str] = []
        for path in sorted(SOURCE_ROOT.rglob("*.py")):
            if path == constants:
                continue
            src = path.read_text(encoding="utf-8")
            if "NO_CYPHER_RESULTS = " in src:  # 再次**定义**（而非 import）
                offenders.append(str(path.relative_to(PROJECT_ROOT)))
        assert not offenders, (
            f"NO_CYPHER_RESULTS 在 constants.py 之外又被定义：{offenders}\n"
            "请改为 `from ...constants import NO_CYPHER_RESULTS`（单一事实来源）。"
        )

    def test_no_cypher_results_is_chinese(self):
        """统一后的文案为中文（面向中文用户，与实现主路径一致）。"""
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.constants import (
            NO_CYPHER_RESULTS,
        )

        assert NO_CYPHER_RESULTS
        assert any("找不到" in str(item) for item in NO_CYPHER_RESULTS)


# ---------------------------------------------------------------------------
# 3. 导出模式（统一为 eager 导出）
# ---------------------------------------------------------------------------


class TestEagerExports:
    def test_packages_export_eagerly(self):
        """迁移后包为顶层 eager 导出，不应再有 `__getattr__` 惰性样板。

        R5.2：infrastructure/knowledge 兼容 shim 已下线，检查范围收敛。
        """
        for rel in ("interfaces/http/__init__.py",):
            src = (SOURCE_ROOT / rel).read_text(encoding="utf-8")
            assert "__getattr__" not in src, f"{rel} 仍残留惰性导出样板"

    def test_legacy_lazy_helper_removed(self):
        """共享惰性导出助手仅服务于遗留领域隔离，应已删除。"""
        assert not (SOURCE_ROOT / "infrastructure" / "core" / "lazy.py").exists()


# ---------------------------------------------------------------------------
# 4. 断链 import（使模块**永久无法导入**的死 import）
# ---------------------------------------------------------------------------


class TestNoBrokenImports:
    """扫描遗留家族，确保没有「目标模块不存在」的 import。

    批次 13 实测修复：`ps_genai_agents/components/guardrails/prompts.py`
    有一条 `from ..utils.utils import ...`——该 `utils` 包**根本不存在**，
    且被导入符号从未使用。它使整个 `ps_genai_agents` 家族无法导入，
    任何静态分析 / import 巡检都会在此报错。
    """

    def test_ps_genai_agents_family_imports_cleanly(self):
        import importlib
        import pkgutil

        package = importlib.import_module(
            "agentix.application.agents.kg_sub_graph.ps_genai_agents"
        )
        broken = []
        for module_info in pkgutil.walk_packages(
            package.__path__, package.__name__ + "."
        ):
            try:
                importlib.import_module(module_info.name)
            except Exception as exc:  # noqa: BLE001 - 正是要收集失败
                broken.append((module_info.name, f"{type(exc).__name__}: {exc}"))
        assert not broken, (
            "以下模块存在断链 import（目标模块不存在或导入期报错）：\n"
            + "\n".join(f"  {name}: {err}" for name, err in broken)
        )

    def test_guardrails_prompt_builder_is_usable(self):
        """修复后该构造器必须真的可用（此前连 import 都做不到）。"""
        from agentix.application.agents.kg_sub_graph.ps_genai_agents.components.guardrails.prompts import (
            create_guardrails_prompt_template,
        )

        template = create_guardrails_prompt_template(
            graph=None, scope_description="法律领域：法规、法条、案由"
        )
        messages = template.format_messages(question="民法典第577条是什么？")
        assert len(messages) == 2
        assert messages[0].content  # system
        assert "参考此范围描述来决策" in str(messages[1].content)
