"""p75 Stage M·M6 收口：三簇 shim + 全部旧路径清尾（终态棘轮）。

M6（2026-10-03）把迁移期的 strangler 转发层整批拆除，本测试钉死**终态**：

1. **零 ``_TARGET``**——全仓（agentix/ 生产源）模块级 ``_TARGET`` 转发常量 == 0
   （p36 曾以同名异值白名单放行 shim 模板，白名单条目已随删清零）；
2. **零旧路径 import 面**——agentix/ + tests/ + scripts/ + evals/ + kb_ingest/
   + 根入口 *.py 的静态 import 语句对 20 个旧 dotted 前缀零命中（equality
   棘轮：新增旧路径 import 即红）；
3. **删除面到位**——三簇目录 + M0–M6 旧 shim 文件/目录全部不存在（与 p69
   manifest / p70–p74 守卫互补，此处做全量收口断言）；
4. **生产源零旧斜杠路径字符串**——agentix/ 内不得再出现旧路径 slash 串
   （tests/ 的守卫清单属留档，不在扫描面）；
5. **门面消失 + 单一事实来源**——capabilities 门面不再导出 tools；``import
   agentix.agent`` 不拖起任何旧子包；``domain.status`` 只剩 RequestStatus。

方案：架构重构统一执行链方案_v2.md §5（M6 删三簇）/ §6 #12（shim 目录棘轮
终态空集，见 p66）/ §7（equality 棘轮基线只减不增）。
"""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

REPO = Path(__file__).resolve().parents[1]

#: 旧路径 dotted 前缀（M0–M6 全批次；含 package-form ``from ... import X`` 展开面）
OLD_DOTTED = (
    "agentix.agent.core",
    "agentix.agent.executor",
    "agentix.agent.multiagent",
    "agentix.agent.planner",
    "agentix.agent.replanner",
    "agentix.agent.verifier",
    "agentix.agent.hitl",
    "agentix.application.agents.lg_builder",
    "agentix.application.agents.lg_states",
    "agentix.application.agents.lg_supervisor",
    "agentix.application.services.control_plane",
    "agentix.application.services.hitl_bridge",
    "agentix.capabilities.tools",
    "agentix.infrastructure.tools",
    "agentix.mcp",
    "agentix.domain.agent_types",
    "agentix.domain.ports.agent_core",
    "agentix.retrieval.domain.policies.legal_effectiveness",
    "agentix.retrieval.domain.services.grounding_verify",
)

#: 静态 import 面扫描根（archive/ 为冻结历史副本，不入被守护面；web/ 同 p66）
IMPORT_SCAN_ROOTS = ("agentix", "tests", "scripts", "evals", "kb_ingest")

#: 应当**不存在**的旧路径（文件或目录）
DELETED_PATHS = [
    # M2/M3：agent 子包执行件（整目录）
    "agentix/agent/core",
    "agentix/agent/executor",
    "agentix/agent/multiagent",
    "agentix/agent/planner",
    "agentix/agent/replanner",
    "agentix/agent/verifier",
    "agentix/agent/hitl",
    # M2：application 侧 langgraph 三件
    "agentix/application/agents/lg_builder.py",
    "agentix/application/agents/lg_states.py",
    "agentix/application/agents/lg_supervisor.py",
    # M3/M4：application/services 两 shim
    "agentix/application/services/control_plane.py",
    "agentix/application/services/hitl_bridge.py",
    # M1/M6 三簇
    "agentix/capabilities/tools",
    "agentix/infrastructure/tools",
    "agentix/mcp",
    # M0 契约旧路径
    "agentix/domain/agent_types.py",
    "agentix/domain/ports/agent_core.py",
    # M5 迁出件
    "agentix/retrieval/domain/policies/legal_effectiveness.py",
    "agentix/retrieval/domain/services/grounding_verify.py",
]

#: 旧路径斜杠串（生产源文本扫描面；tests/ 留档清单不在此列）
OLD_SLASH = (
    "agentix/agent/core/",
    "agentix/agent/executor/",
    "agentix/agent/multiagent/",
    "agentix/agent/planner/",
    "agentix/agent/replanner/",
    "agentix/agent/verifier/",
    "agentix/agent/hitl/",
    "agentix/application/agents/lg_builder",
    "agentix/application/agents/lg_states",
    "agentix/application/agents/lg_supervisor",
    "agentix/application/services/control_plane",
    "agentix/application/services/hitl_bridge",
    "agentix/capabilities/tools/",
    "agentix/infrastructure/tools/",
    "agentix/mcp/",
    "agentix/domain/agent_types",
    "agentix/domain/ports/agent_core",
    "agentix/retrieval/domain/policies/legal_effectiveness",
    "agentix/retrieval/domain/services/grounding_verify",
)

SKIP_DIRS = {".venv", "__pycache__", "node_modules", ".git", "web", "archive"}


def _py_files(*roots: str) -> List[Path]:
    out: List[Path] = []
    for root in roots:
        base = REPO / root
        if base.is_dir():
            out.extend(
                p for p in base.rglob("*.py")
                if not any(part in SKIP_DIRS for part in p.relative_to(REPO).parts)
            )
    return out


def _parse(path: Path):
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return None


def _imported_modules(tree: ast.AST) -> List[tuple[int, str]]:
    out: List[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((node.lineno, a.name) for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.append((node.lineno, node.module))
            out.extend(
                (node.lineno, f"{node.module}.{a.name}")
                for a in node.names
                if a.name != "*"
            )
    return out


class TestZeroTargetShims:
    def test_no_module_level_target_in_production(self) -> None:
        """零 ``_TARGET`` 转发常量（equality 棘轮 == 0）。"""
        offenders: List[str] = []
        for f in _py_files("agentix"):
            tree = _parse(f)
            if tree is None:
                continue
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "_TARGET"
                    for t in node.targets
                ):
                    offenders.append(f.relative_to(REPO).as_posix())
        assert offenders == [], f"M6 后仍出现 _TARGET 转发 shim：{offenders}"


class TestZeroOldImportSurface:
    def test_no_static_imports_of_old_paths(self) -> None:
        """静态 import 面 == 0（equality 棘轮：新增旧路径 import 即红）。"""
        offenders: List[str] = []
        for f in _py_files(*IMPORT_SCAN_ROOTS):
            tree = _parse(f)
            if tree is None:
                continue
            rel = f.relative_to(REPO).as_posix()
            for lineno, name in _imported_modules(tree):
                for old in OLD_DOTTED:
                    if name == old or name.startswith(old + "."):
                        offenders.append(f"{rel}:{lineno} -> {name}")
        for f in sorted(REPO.glob("*.py")):
            tree = _parse(f)
            if tree is None:
                continue
            rel = f.relative_to(REPO).as_posix()
            for lineno, name in _imported_modules(tree):
                for old in OLD_DOTTED:
                    if name == old or name.startswith(old + "."):
                        offenders.append(f"{rel}:{lineno} -> {name}")
        assert offenders == [], (
            "出现旧路径 static import（M6 已删，会直接 ImportError）："
            f"{offenders}"
        )


class TestDeletedPathsGone:
    @pytest.mark.parametrize("rel", DELETED_PATHS)
    def test_path_absent(self, rel: str) -> None:
        assert not (REPO / rel).exists(), f"M6 已完成但旧路径仍存在：{rel}"

    @pytest.mark.parametrize(
        "dotted",
        [
            "agentix.capabilities.tools",
            "agentix.infrastructure.tools",
            "agentix.mcp",
            "agentix.domain.agent_types",
            "agentix.application.services.control_plane",
        ],
    )
    def test_old_dotted_import_raises(self, dotted: str) -> None:
        with pytest.raises(ImportError):
            importlib.import_module(dotted)


class TestProductionSlashPathsGone:
    def test_no_old_slash_paths_in_source(self) -> None:
        """agentix/ 生产源不得再出现旧路径斜杠串（含注释/docstring——
        历史留档统一走 dotted 形式或 p69/p70–p74 守卫清单）。"""
        offenders: List[str] = []
        for f in _py_files("agentix"):
            text = f.read_text(encoding="utf-8")
            for i, line in enumerate(text.splitlines(), 1):
                for s in OLD_SLASH:
                    if s in line:
                        offenders.append(f"{f.relative_to(REPO).as_posix()}:{i} {s}")
        assert offenders == [], f"生产源残留旧路径字符串：{offenders}"


class TestFacadesGone:
    def test_capabilities_facade_has_no_tools(self) -> None:
        cap = importlib.import_module("agentix.capabilities")
        assert not hasattr(cap, "tools"), "capabilities 门面不得再导出 tools"
        assert "agentix.capabilities.tools" not in sys.modules

    def test_agent_package_import_drags_no_old_subpackages(self) -> None:
        code = (
            "import sys\n"
            "import agentix.agent\n"
            "import agentix.capabilities\n"
            "banned = [m for m in sys.modules if m.startswith(\n"
            "    ('agentix.agent.core', 'agentix.agent.executor',\n"
            "     'agentix.agent.planner', 'agentix.agent.replanner',\n"
            "     'agentix.agent.verifier', 'agentix.agent.hitl',\n"
            "     'agentix.agent.multiagent', 'agentix.capabilities.tools',\n"
            "     'agentix.infrastructure.tools', 'agentix.mcp'))]\n"
            "assert not banned, banned\n"
            "print('FACADE_CLEAN')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.returncode == 0, f"stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}"
        assert "FACADE_CLEAN" in proc.stdout

    def test_domain_status_single_source(self) -> None:
        st = importlib.import_module("agentix.domain.status")
        assert hasattr(st, "RequestStatus")
        assert not hasattr(st, "ExitStatus"), "ExitStatus 已收敛到 domain.runtime.lifecycle"
        assert not hasattr(st, "ExitReason"), "ExitReason 已收敛到 domain.runtime.lifecycle"
