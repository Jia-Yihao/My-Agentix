"""代码卫生守卫：把审计中修掉的几类"静默退化"固化成回归测试。

本文件收录的问题都有同一个特征——**不会让 CI 变红，但会让判断失真**：

1. **pydantic 旧式 ``class Config``**：Pydantic V2 起弃用、V3 将移除。
   它只在导入时打 ``PydanticDeprecatedSince20`` 警告，落在 warnings summary
   里很容易被当成噪声忽略（实测 10 处）。现统一改为 ``model_config = ConfigDict(...)``。

2. **裸 ``except:``**：连 ``KeyboardInterrupt`` / ``SystemExit`` 一起吞掉。

3. **可变默认参数**（``def f(x=[])``）：默认值在定义期求值一次，跨调用共享。

4. **脚本型测试模块暴露 ``test_*`` 函数**：这是最隐蔽的一类——
   ``tests/test_agent_routing.py`` / ``tests/test_heuristic_router.py`` 是
   需要真实 LLM 的**脚本**，模块级有 ``pytest.skip(..., allow_module_level=True)``。
   但它们的辅助函数叫 ``test_routing`` / ``test_query`` 且**带必需位置参数**。
   默认情况下模块被 skip 所以没事；一旦按文档设置
   ``LEGELBOT_RUN_INTEGRATION_TESTS=1`` 打开"逃逸开关"，pytest 就会把它们
   收集成用例并报 fixture 错误——"开启集成测试"的结果是**报错**而不是运行。
   守卫口径：凡是有 ``allow_module_level=True`` 跳过的模块，不得再定义
   顶层 ``test_*`` 函数。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_ROOT = REPO_ROOT / "agentix"
TESTS_ROOT = REPO_ROOT / "tests"

_SKIP_ALL_MODULE_RE = re.compile(r"allow_module_level\s*=\s*True")


def _iter_python_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _read_source(path: Path) -> str:
    """读源码，容忍 BOM。

    ``encoding="utf-8-sig"`` 而不是 ``"utf-8"``：仓库里曾有 48 个 .py 带
    UTF-8 BOM（已剥离），``ast.parse`` 遇到 ``\\ufeff`` 会直接
    ``SyntaxError: invalid non-printable character U+FEFF``。守卫本身不该因为
    待检查文件的编码问题而崩掉——那会把"检查失败"伪装成"收集错误"。
    """
    return path.read_text(encoding="utf-8-sig")


class TestNoUtf8Bom:
    """``.py`` 源文件不得带 UTF-8 BOM。

    BOM 对 CPython 导入无害（词法器会跳过），但它会让**任何**用
    ``read_text() + ast.parse()`` 的工具链报错——包括本文件的守卫、
    ``pyupgrade``、部分代码生成器。实测仓库曾有 48 个文件带 BOM，
    正是本守卫在开发过程中被自己的 SyntaxError 抓出来的。
    """

    def test_no_bom_in_python_sources(self):
        offenders = [
            str(p.relative_to(REPO_ROOT))
            for root in (PKG_ROOT, TESTS_ROOT, REPO_ROOT / "evals", REPO_ROOT / "scripts")
            if root.exists()
            for p in _iter_python_files(root)
            if p.read_bytes()[:3] == b"\xef\xbb\xbf"
        ]
        assert not offenders, "以下 .py 文件带 UTF-8 BOM（应为 utf-8 无 BOM）：\n  " + "\n  ".join(
            offenders
        )


# ----------------------------------------------------------- pydantic 旧式 Config


class TestNoPydanticLegacyConfig:
    def test_no_class_based_config_in_package(self):
        offenders = []
        for path in _iter_python_files(PKG_ROOT):
            tree = ast.parse(_read_source(path), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == "Config":
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
        assert not offenders, (
            "发现 pydantic 旧式 class Config（V3 将移除，改用 model_config = ConfigDict(...)）：\n  "
            + "\n  ".join(offenders)
        )

    def test_config_dict_is_imported_where_used(self):
        """防空真 + 防"用了 ConfigDict 但没导入"（会 NameError）。"""
        users = []
        for path in _iter_python_files(PKG_ROOT):
            text = _read_source(path)
            if "model_config = ConfigDict(" in text:
                users.append(path)
        assert users, "没有任何文件使用 ConfigDict——本守卫的前提失效，请检查"
        for path in users:
            text = _read_source(path)
            assert re.search(r"^from pydantic import .*\bConfigDict\b", text, re.M), (
                f"{path.relative_to(REPO_ROOT)} 使用了 ConfigDict 但没有从 pydantic 导入"
            )


# ----------------------------------------------------------- 裸 except / 可变默认值


class TestDangerousPythonPatterns:
    def test_no_bare_except(self):
        offenders = []
        for root in (PKG_ROOT, TESTS_ROOT, REPO_ROOT / "evals"):
            if not root.exists():
                continue
            for path in _iter_python_files(root):
                for lineno, line in enumerate(
                    _read_source(path).splitlines(), 1
                ):
                    stripped = line.strip()
                    if stripped in ("except:", "except :"):
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
        assert not offenders, "发现裸 except:（会吞掉 KeyboardInterrupt/SystemExit）：\n  " + "\n  ".join(
            offenders
        )

    def test_no_mutable_default_arguments(self):
        offenders = []
        for root in (PKG_ROOT, REPO_ROOT / "evals"):
            if not root.exists():
                continue
            for path in _iter_python_files(root):
                tree = ast.parse(_read_source(path), filename=str(path))
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    defaults = list(node.args.defaults) + [
                        d for d in node.args.kw_defaults if d is not None
                    ]
                    for default in defaults:
                        if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                            offenders.append(
                                f"{path.relative_to(REPO_ROOT)}:{node.lineno} "
                                f"def {node.name}(...) 使用了可变默认值"
                            )
        assert not offenders, "发现可变默认参数：\n  " + "\n  ".join(offenders)

    def test_no_assert_used_as_validation_in_production(self):
        """生产代码不得用 ``assert`` 承担输入校验/不变量。

        ``assert`` 在 ``python -O`` / ``PYTHONOPTIMIZE=1`` 下会被**整条剥离**，
        校验随之静默消失。审计中实测 11 处，其中 5 处是 pydantic
        ``@field_validator`` 里的类型约束（``text2cypher`` 的 schema 校验正是
        防「LLM 幻觉出非法属性类型」的关键一层），另 3 处校验 LLM 可控的
        ``node_or_rel``。全部已改为显式 ``raise ValueError`` / ``RuntimeError``。

        注意：本守卫只看 ``agentix/``（生产代码）；测试里用 assert 是正确的。
        """
        offenders = []
        for path in _iter_python_files(PKG_ROOT):
            tree = ast.parse(_read_source(path), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Assert):
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno} "
                        f"{ast.unparse(node.test)[:70]}"
                    )
        assert not offenders, (
            f"生产代码中发现 {len(offenders)} 处 assert（-O 下会被剥离，校验会静默失效），"
            "请改为显式 raise：\n  " + "\n  ".join(offenders)
        )


# ----------------------------------------------------------- 脚本型模块不得被收集


def _module_level_skipped_files():
    """返回所有"整模块 skip"的测试文件（脚本型）。"""
    out = []
    for path in _iter_python_files(TESTS_ROOT):
        text = _read_source(path)
        if _SKIP_ALL_MODULE_RE.search(text):
            out.append(path)
    return out


class TestScriptModulesAreNotCollectable:
    def test_there_is_at_least_one_script_module(self):
        """防空真：若将来这些脚本都改成了真正的测试，本守卫应被显式移除而不是空跑通过。"""
        assert _module_level_skipped_files(), "没有任何整模块 skip 的脚本型测试——请确认是否已重构"

    def test_script_modules_expose_no_top_level_test_functions(self):
        """有整模块 skip 的脚本文件，不得再定义顶层 ``test_*`` 函数。

        否则打开 ``LEGELBOT_RUN_INTEGRATION_TESTS`` 会让 pytest 收集到
        带必需位置参数的"用例"，直接报 fixture 错误。
        """
        offenders = []
        for path in _module_level_skipped_files():
            tree = ast.parse(_read_source(path), filename=str(path))
            for node in tree.body:  # 只看顶层
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
                    "test_"
                ):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} def {node.name}")
        assert not offenders, (
            "脚本型测试模块暴露了顶层 test_* 函数（打开逃逸开关会变成 fixture 错误），"
            "请改名为 run_*：\n  " + "\n  ".join(offenders)
        )


# ----------------------------------------------------------- 回归：历史缺陷不得复现


class TestHistoricalDefectsStayFixed:
    """针对本次审计修复的具体缺陷做定点回归（比通用规则更难被"绕过"）。"""

    def test_ablation_and_metric_helpers_still_exist(self):
        """防空真：本文件依赖的源码结构若被重构，守卫应显式报警而非静默失效。"""
        from agentix.config import ablation

        assert hasattr(ablation, "WIRED_FLAGS")
        assert hasattr(ablation, "is_enabled")

    @pytest.mark.parametrize(
        "relative_path,needle,why",
        [
            (
                # Stage M·M1：实现已迁 agentix/tools/，旧路径 capabilities/ 是 shim
                "agentix/tools/definitions.py",
                "FORBIDDEN_RISK_LEVEL",
                "L3 必须保持「代码层禁止注册」的真实守卫",
            ),
            (
                "agentix/tools/gateway/human_review.py",
                "_ARG_ESCALATION_RULES",
                "参数提权必须取最高等级，不能依赖 dict 顺序",
            ),
            (
                "agentix/tools/__init__.py",
                "闸门粒度是「每次实例化」，不是「每次调用」",
                "必须说明 get_tool 拿不到 args 这一限制",
            ),
        ],
    )
    def test_fix_marker_present(self, relative_path, needle, why):
        path = REPO_ROOT / relative_path
        assert path.exists(), f"文件不存在：{relative_path}"
        assert needle in _read_source(path), f"{relative_path} 中缺少修复标记：{why}"
