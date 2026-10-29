"""P73：Stage M·M4 控制平面迁移契约
（`application/services/control_plane.py` → `agentix/runtime/control_plane.py`）。

覆盖：
1. 目标位为真实实现；旧路径（迁移期 `sys.modules` 别名式 shim）已随 **M6
   （2026-10-03）删除**——断言方向翻转：文件不存在、``import`` 抛 ``ImportError``；
2. 符号齐全（含 p69 钉的 `RunState`/`TERMINAL_STATES` 规范位归属）；
3. monkeypatch 只落规范位（phase25 `_invoke_tracked` 测试形态改指新路径）、
   单例注册表唯一入口；
4. Runtime 簇自包含：runtime/** 模块级（import 期）agentix 边只允许
   `agentix.runtime` / `agentix.domain` / `agentix.config`——`TokenBudget` 仅以
   `TYPE_CHECKING` 注解边存在（先例 `runtime/state.py` 对 `steps`），
   运行期零 application 边；
5. 双向加载序子进程（旧路径断言抛错，规范位两序皆可）。
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

REPO = Path(__file__).resolve().parents[1]

OLD_REL = "agentix/application/services/control_plane.py"
NEW_REL = "agentix/runtime/control_plane.py"
OLD_MOD = "agentix.application.services.control_plane"
NEW_MOD = "agentix.runtime.control_plane"

#: runtime 模块级（import 期）允许的 agentix 边——Runtime 簇自包含
RUNTIME_MODULE_LEVEL_ALLOWED = ("agentix.runtime", "agentix.domain", "agentix.config")

SYMBOLS = [
    "AdmissionDenied",
    "AgentRun",
    "ControlPlane",
    "get_control_plane",
    "reset_control_plane",
    "RunState",
    "TERMINAL_STATES",
]


def _top_level_imports(tree: ast.Module) -> List[str]:
    """模块级 import 期边：跳过 ``if TYPE_CHECKING`` 块（不求值），
    其余顶层语句（含普通 if 体内一层）计入。"""
    out: List[str] = []

    def walk(stmts, depth: int = 0) -> None:
        for n in stmts:
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                if isinstance(n, ast.ImportFrom) and n.level == 0:
                    if n.module:
                        out.append(n.module)
                else:
                    out.extend(a.name for a in n.names)
            elif isinstance(n, ast.If) and depth < 3:
                is_tc = isinstance(n.test, ast.Name) and n.test.id == "TYPE_CHECKING"
                if not is_tc:
                    walk(n.body, depth + 1)
                    walk(n.orelse, depth + 1)

    walk(tree.body)
    return out


class TestTargetLayout:
    def test_new_home_exists_and_is_real_implementation(self) -> None:
        assert (REPO / NEW_REL).is_file(), "M4 规范位缺失"
        text = (REPO / NEW_REL).read_text(encoding="utf-8")
        assert "_TARGET =" not in text, "新家不允许是 shim"

    def test_old_path_deleted(self) -> None:
        assert not (REPO / OLD_REL).exists(), (
            "M6 已完成但旧路径 control_plane shim 仍存在"
        )

    def test_old_path_import_raises(self) -> None:
        with pytest.raises(ImportError):
            __import__(OLD_MOD, fromlist=["*"])


class TestSymbolSurface:
    @pytest.mark.parametrize("name", SYMBOLS)
    def test_symbol_on_canonical(self, name: str) -> None:
        new = __import__(NEW_MOD, fromlist=["*"])
        assert hasattr(new, name), f"规范位 {NEW_MOD}.{name} 缺失"

    def test_package_form_import_gone(self) -> None:
        """phase25 形态 ``from agentix.application.services import control_plane``
        M6 后应抛错（迁移期经子包属性解析到别名 shim）。动态化书写以保持
        p75 静态 import 面棘轮为零。"""
        import importlib

        with pytest.raises(ImportError):
            importlib.import_module("agentix.application.services.control_plane")
        services = importlib.import_module("agentix.application.services")
        assert not hasattr(services, "control_plane")


class TestMonkeypatchRoundtrip:
    def test_patch_on_canonical(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import agentix.runtime.control_plane as new

        with pytest.raises(ImportError):
            __import__(OLD_MOD, fromlist=["*"])

        sentinel = object()
        monkeypatch.setattr(new, "M73_SENTINEL", sentinel, raising=False)
        assert new.M73_SENTINEL is sentinel

    def test_get_control_plane_patch_visible(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """phase25 `_invoke_tracked` 测试形态（M6 口径）：补丁只打在规范位
        ``agentix.runtime.control_plane`` 上。"""
        import agentix.runtime.control_plane as new

        fake = object()
        monkeypatch.setattr(new, "get_control_plane", lambda: fake)
        assert new.get_control_plane() is fake

    def test_singleton_registry_unique_entry(self) -> None:
        from agentix.runtime.control_plane import (
            get_control_plane,
            reset_control_plane,
        )

        reset_control_plane()
        try:
            plane = get_control_plane()
            plane.spawn("p73-agent")
            assert get_control_plane() is plane
            assert any(r.agent_name == "p73-agent" for r in get_control_plane().list_runs())
        finally:
            reset_control_plane()


class TestRuntimeSelfContained:
    def test_module_level_edges_stay_in_cluster(self) -> None:
        offenders: List[str] = []
        for f in sorted((REPO / "agentix/runtime").rglob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for name in _top_level_imports(tree):
                if not name.startswith("agentix"):
                    continue
                if not name.startswith(RUNTIME_MODULE_LEVEL_ALLOWED):
                    offenders.append(f"{f.relative_to(REPO).as_posix()} -> {name}")
        assert offenders == [], (
            "runtime 模块级出现跨簇 import 期边（Runtime 簇须运行期自包含，"
            f"跨簇引用请走 TYPE_CHECKING/函数内延迟）: {offenders}"
        )

    def test_control_plane_token_budget_is_type_checking_only(self) -> None:
        tree = ast.parse((REPO / NEW_REL).read_text(encoding="utf-8"))
        tc_modules: List[str] = []
        for n in tree.body:
            if isinstance(n, ast.If) and isinstance(n.test, ast.Name) and n.test.id == "TYPE_CHECKING":
                for b in ast.walk(n):
                    if isinstance(b, ast.ImportFrom) and b.module:
                        tc_modules.append(b.module)
        assert "agentix.domain.runtime.token_budget" in tc_modules, (
            "TokenBudget 应仅存在于 if TYPE_CHECKING 块（注解专用，"
            "M2-3 拆边后规范位在 domain）"
        )
        assert "agentix.application.services.token_budget" not in tc_modules, (
            "M2-3 拆边：runtime → application 的类型引用边已消失，"
            "TokenBudget 不得再从 application 路径进入 TYPE_CHECKING"
        )
        assert "agentix.application.services.token_budget" not in _top_level_imports(tree), (
            "TokenBudget 不得出现在 import 期（会破 Runtime 簇自包含）"
        )

    def test_budget_annotation_is_lazy_string(self) -> None:
        from agentix.runtime.control_plane import AgentRun

        ann = AgentRun.__annotations__.get("budget", "")
        assert isinstance(ann, str) and "TokenBudget" in ann, ann
        run = AgentRun(run_id="r1", agent_name="a")  # 无 TokenBudget 也可构造
        assert run.budget is None


class TestLoadOrderSafety:
    def _run(self, code: str) -> None:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.returncode == 0, f"stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}"

    def test_old_path_gone_new_path_works(self) -> None:
        self._run(
            "import importlib\n"
            "try:\n"
            "    importlib.import_module('agentix.application.services.control_plane')\n"
            "except ImportError:\n"
            "    pass\n"
            "else:\n"
            "    raise SystemExit('旧路径仍可导入')\n"
            "from agentix.runtime.control_plane import ControlPlane\n"
            "assert ControlPlane\n"
            "print('P73_CANONICAL_OK')\n"
        )

    def test_new_path_first(self) -> None:
        self._run(
            "import agentix.runtime.control_plane as n\n"
            "from agentix.runtime.control_plane import ControlPlane\n"
            "assert n.ControlPlane is ControlPlane\n"
        )

    def test_runtime_package_first(self) -> None:
        self._run(
            "import agentix.runtime\n"
            "from agentix.runtime.control_plane import get_control_plane\n"
            "assert get_control_plane\n"
        )
