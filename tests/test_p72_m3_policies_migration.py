"""P72：Stage M·M3 策略件迁移契约（agent/{planner,replanner,verifier,hitl} +
application/services/hitl_bridge.py + capabilities/tools/guardrails.py → agentix/policies/）。

覆盖：
1. 目标位落成（planner 归并 simple+retry / verifier / hitl(sidecar+bridge) / guardrails）；
2. 旧路径清尾（**M6，2026-10-03**）：迁移期 7 个 sys.modules 别名式 shim +
   4 个 PEP 562 包门面已全部删除——断言方向翻转为：文件不存在、``import``
   抛 ``ImportError``、规范位单一入口；
3. 新家自足：policies/** 不回引 M3 旧路径（hitl_bridge 的外部依赖已随 M6
   codemod 改指规范位 tools/，breakpoint_resume 上行引用保留）；
4. executor（tools 位）M6 起直连 ``agentix.policies.guardrails``——p66 守卫
   #3 以 ``TOOLS_GUARDRAILS_EDGE_ALLOWED`` 窄豁免登记（蓝图 §5 Tool
   Gateway 链含 Guardrails 步）；
5. domain/policies（纯契约）与顶层 policies/（执行件）分界不混。
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

REPO = Path(__file__).resolve().parents[1]

#: 旧路径模块 → 新路径模块（迁移期 sys.modules 别名 shim；M6 已删）
MODULE_SHIMS = {
    "agentix/agent/planner/simple.py": "agentix/policies/planner/simple.py",
    "agentix/agent/replanner/retry.py": "agentix/policies/planner/retry.py",
    "agentix/agent/verifier/layered.py": "agentix/policies/verifier/layered.py",
    "agentix/agent/verifier/structural.py": "agentix/policies/verifier/structural.py",
    "agentix/agent/hitl/sidecar.py": "agentix/policies/hitl/sidecar.py",
    "agentix/application/services/hitl_bridge.py": "agentix/policies/hitl/hitl_bridge.py",
    "agentix/capabilities/tools/guardrails.py": "agentix/policies/guardrails/guardrails.py",
}

MODULE_PAIRS = [
    ("agentix.agent.planner.simple", "agentix.policies.planner.simple"),
    ("agentix.agent.replanner.retry", "agentix.policies.planner.retry"),
    ("agentix.agent.verifier.layered", "agentix.policies.verifier.layered"),
    ("agentix.agent.verifier.structural", "agentix.policies.verifier.structural"),
    ("agentix.agent.hitl.sidecar", "agentix.policies.hitl.sidecar"),
    ("agentix.application.services.hitl_bridge", "agentix.policies.hitl.hitl_bridge"),
    ("agentix.capabilities.tools.guardrails", "agentix.policies.guardrails.guardrails"),
]

#: 迁移期旧包门面（lazy）→ (目标包, 迁移前导出面留档；M6 后门面已删)
PKG_SHIMS = {
    "agentix/agent/planner/__init__.py": (
        "agentix.policies.planner",
        ["SingleStepPlanner", "DeclaredStepsPlanner"],
    ),
    "agentix/agent/replanner/__init__.py": (
        "agentix.policies.planner",
        ["RetryReplanner", "failure_key"],
    ),
    "agentix/agent/verifier/__init__.py": (
        "agentix.policies.verifier",
        ["StructuralVerifier", "LayeredVerifier"],
    ),
    "agentix/agent/hitl/__init__.py": (
        "agentix.policies.hitl",
        [
            "ReviewDecisionAction",
            "ReviewerSidecar",
            "SidecarReviewRequest",
            "SidecarReviewResult",
        ],
    ),
}

NEW_PKG_FILES = (
    "agentix/policies/__init__.py",
    "agentix/policies/planner/__init__.py",
    "agentix/policies/planner/simple.py",
    "agentix/policies/planner/retry.py",
    "agentix/policies/verifier/__init__.py",
    "agentix/policies/verifier/layered.py",
    "agentix/policies/verifier/structural.py",
    "agentix/policies/hitl/__init__.py",
    "agentix/policies/hitl/sidecar.py",
    "agentix/policies/hitl/hitl_bridge.py",
    "agentix/policies/guardrails/__init__.py",
    "agentix/policies/guardrails/guardrails.py",
)

#: M3 旧路径前缀（policies/** 禁引；M6 后文件已删，本扫描为永久棘轮）
OLD_PREFIXES = (
    "agentix.agent.planner",
    "agentix.agent.replanner",
    "agentix.agent.verifier",
    "agentix.agent.hitl",
    "agentix.application.services.hitl_bridge",
    "agentix.capabilities.tools.guardrails",
)


class TestTargetLayout:
    @pytest.mark.parametrize("rel", NEW_PKG_FILES)
    def test_new_home_exists(self, rel: str) -> None:
        assert (REPO / rel).is_file(), f"M3 规范位缺失: {rel}"

    @pytest.mark.parametrize(
        "rel",
        [
            "agentix/policies/planner/simple.py",
            "agentix/policies/planner/retry.py",
            "agentix/policies/verifier/layered.py",
            "agentix/policies/hitl/sidecar.py",
            "agentix/policies/hitl/hitl_bridge.py",
            "agentix/policies/guardrails/guardrails.py",
        ],
    )
    def test_new_home_is_real_implementation(self, rel: str) -> None:
        text = (REPO / rel).read_text(encoding="utf-8")
        assert "_TARGET =" not in text, f"新家不允许是 shim: {rel}"

    def test_policies_root_init_import_free(self) -> None:
        """policies 包门面零 import：策略件被多层引用，eager 汇总易踩半初始化窗口。"""
        tree = ast.parse((REPO / "agentix/policies/__init__.py").read_text(encoding="utf-8"))
        imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        assert imports == []

    def test_domain_policies_still_separate(self) -> None:
        """顶层 policies/（执行件）与 domain/policies/（纯契约值对象）不混。"""
        assert (REPO / "agentix/domain/policies/retry.py").is_file()
        assert not (REPO / "agentix/policies/retry.py").exists(), (
            "replanner 的 retry.py 应归并进 policies/planner/，不得在 policies/ 顶层"
        )


class TestOldPathShimsDeleted:
    """M3 迁移期 7 个别名式 shim + 4 个包门面：M6 后全数删除。"""

    @pytest.mark.parametrize("old_rel,new_rel", sorted(MODULE_SHIMS.items()))
    def test_shim_file_deleted(self, old_rel: str, new_rel: str) -> None:
        assert not (REPO / old_rel).exists(), f"M6 已完成但旧路径 shim 仍存在: {old_rel}"
        assert (REPO / new_rel).is_file(), f"规范位缺失: {new_rel}"

    @pytest.mark.parametrize("old,new", MODULE_PAIRS)
    def test_old_import_raises_new_importable(self, old: str, new: str) -> None:
        with pytest.raises(ImportError):
            __import__(old, fromlist=["*"])
        assert __import__(new, fromlist=["*"]) is not None

    @pytest.mark.parametrize("rel", sorted(PKG_SHIMS))
    def test_package_facade_deleted(self, rel: str) -> None:
        assert not (REPO / rel).exists(), f"M6 已完成但包门面仍存在: {rel}"


class TestPackageBehavior:
    @pytest.mark.parametrize("old,new", [
        ("agentix.agent.planner", "agentix.policies.planner"),
        ("agentix.agent.replanner", "agentix.policies.planner"),
        ("agentix.agent.verifier", "agentix.policies.verifier"),
        ("agentix.agent.hitl", "agentix.policies.hitl"),
    ])
    def test_old_package_gone_new_package_exported(self, old: str, new: str) -> None:
        with pytest.raises(ImportError):
            __import__(old, fromlist=["*"])
        new_pkg = __import__(new, fromlist=["*"])
        assert list(new_pkg.__all__), f"{new} 须有导出面"

    def test_planner_new_package_merges_replanner(self) -> None:
        """replanner 归并入 policies/planner（蓝图 §5），新门面四符号齐。"""
        import agentix.policies.planner as pkg

        for name in ("SingleStepPlanner", "DeclaredStepsPlanner", "RetryReplanner", "failure_key"):
            assert name in pkg.__all__, f"policies.planner 缺 {name}"

    def test_planner_frozen_surface_on_canonical(self) -> None:
        """规范位 planner 导出面含迁移前两符号（归并后另加 replanner 两符号）。"""
        import agentix.policies.planner as pkg

        for name in PKG_SHIMS["agentix/agent/planner/__init__.py"][1]:
            assert name in pkg.__all__


class TestMonkeypatchRoundtrip:
    def test_patch_hitl_bridge_on_canonical_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """旧路径别名传导语义随 M6 删除——patch 只落在规范位上。"""
        import agentix.policies.hitl.hitl_bridge as real

        with pytest.raises(ImportError):
            __import__("agentix.application.services.hitl_bridge", fromlist=["*"])

        sentinel = object()
        monkeypatch.setattr(real, "M72_SENTINEL", sentinel, raising=False)
        assert real.M72_SENTINEL is sentinel

    def test_patch_guardrails_on_canonical_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import agentix.policies.guardrails.guardrails as real

        with pytest.raises(ImportError):
            __import__("agentix.capabilities.tools.guardrails", fromlist=["*"])

        sentinel = object()
        monkeypatch.setattr(real, "M72_SENTINEL", sentinel, raising=False)
        assert real.M72_SENTINEL is sentinel


class TestNewHomeSelfContained:
    def _imported_names(self, f: Path) -> List[str]:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        out: List[str] = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                out.extend(a.name for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                out.append(n.module)
        return out

    def test_policies_never_imports_m3_old_paths(self) -> None:
        offenders: List[str] = []
        for f in sorted((REPO / "agentix/policies").rglob("*.py")):
            for name in self._imported_names(f):
                if name.startswith(OLD_PREFIXES):
                    offenders.append(f"{f.relative_to(REPO).as_posix()} -> {name}")
        assert offenders == [], f"policies/** 回引 M3 旧路径: {offenders}"

    def test_hitl_bridge_dependencies_on_canonical_paths(self) -> None:
        """hitl_bridge 外部依赖已随 M6 codemod 改指规范位 tools/；
        application.services.breakpoint_resume 上行引用保留（M2 起同口径）。"""
        text = (REPO / "agentix/policies/hitl/hitl_bridge.py").read_text(encoding="utf-8")
        assert "agentix.tools.definitions" in text
        assert "agentix.tools.gateway.human_review" in text
        assert "agentix.application.services.breakpoint_resume" in text
        assert "agentix.capabilities.tools" not in text


class TestExecutorUsesGuardrailsCanonicalPath:
    def test_executor_imports_policies_guardrails_directly(self) -> None:
        """tools/gateway/executor.py M6 起直连规范位 guardrails：迁移期经
        capabilities shim 转发以满足 p66 守卫 #3；M6 删 shim 后 p66 以
        ``TOOLS_GUARDRAILS_EDGE_ALLOWED`` 窄豁免登记该边（蓝图 §5）。"""
        text = (REPO / "agentix/tools/gateway/executor.py").read_text(encoding="utf-8")
        assert "agentix.policies.guardrails" in text
        assert "agentix.capabilities.tools" not in text


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

    def test_hitl_bridge_old_path_gone_canonical_works(self) -> None:
        self._run(
            "import importlib\n"
            "for mod in ('agentix.application.services.hitl_bridge',\n"
            "            'agentix.capabilities.tools.guardrails',\n"
            "            'agentix.agent.planner', 'agentix.agent.replanner',\n"
            "            'agentix.agent.verifier', 'agentix.agent.hitl'):\n"
            "    try:\n"
            "        importlib.import_module(mod)\n"
            "    except ImportError:\n"
            "        continue\n"
            "    raise SystemExit(f'旧路径仍可导入: {mod}')\n"
            "from agentix.policies.hitl.hitl_bridge import get_hitl_bridge\n"
            "from agentix.policies.guardrails import get_tool_guardrails\n"
            "import agentix.policies.planner as p\n"
            "import agentix.policies.verifier as v\n"
            "assert get_hitl_bridge and get_tool_guardrails\n"
            "assert p.DeclaredStepsPlanner and p.RetryReplanner\n"
            "assert v.LayeredVerifier and v.StructuralVerifier\n"
            "print('P72_CANONICAL_OK')\n"
        )
