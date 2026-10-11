"""P71：Stage M·M2 编排件迁移契约（agentix/agent + application/agents/lg_* → agentix/orchestration/）。

覆盖：
1. 目标位落成（orchestrator / executor / langgraph / strategies/multi_agent）；
2. 旧路径清尾（**M6，2026-10-03**）：迁移期 8 个模块级 **sys.modules 别名式**
   shim + multiagent PEP 562 包门面已全部删除——断言方向翻转为：文件不存在、
   ``import`` 抛 ``ImportError``、规范位单一入口；
3. 新家自足：orchestration/** 不得回引 M2 旧路径兄弟（lg_prompts 业务 prompt
   留 application，属允许的上行引用）；
4. 双向加载序安全（orchestrator 先 / agent 包先，防 agent.__init__ 回拉环）；
5. lg_prompts 留守：application 内为真实文件而非 shim。
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

REPO = Path(__file__).resolve().parents[1]

# (旧路径, 新路径) — 迁移期为模块级 sys.modules 别名 shim，**M6 已删**
MODULE_SHIMS = {
    "agentix/agent/core/loop.py": "agentix/orchestration/orchestrator.py",
    "agentix/agent/executor/executor.py": "agentix/orchestration/executor.py",
    "agentix/agent/multiagent/bus.py": "agentix/orchestration/strategies/multi_agent/bus.py",
    "agentix/agent/multiagent/manager.py": "agentix/orchestration/strategies/multi_agent/manager.py",
    "agentix/agent/multiagent/peer.py": "agentix/orchestration/strategies/multi_agent/peer.py",
    "agentix/application/agents/lg_builder.py": "agentix/orchestration/langgraph/lg_builder.py",
    "agentix/application/agents/lg_states.py": "agentix/orchestration/langgraph/lg_states.py",
    "agentix/application/agents/lg_supervisor.py": "agentix/orchestration/langgraph/lg_supervisor.py",
}

#: 迁移期旧路径清单（含 multiagent 包门面）——M6 后整批删除
DELETED_OLD_PATHS: List[str] = list(MODULE_SHIMS) + [
    "agentix/agent/multiagent/__init__.py"
]

NEW_PKG_FILES = (
    "agentix/orchestration/__init__.py",
    "agentix/orchestration/langgraph/__init__.py",
    "agentix/orchestration/strategies/__init__.py",
    "agentix/orchestration/strategies/multi_agent/__init__.py",
)
#: 必须零 import 的空门面（环上）；multi_agent/__init__ 是真实 eager 汇总
#: （bus→peer 无环），刻意不在此列。
IMPORT_FREE_INITS = (
    "agentix/orchestration/__init__.py",
    "agentix/orchestration/langgraph/__init__.py",
    "agentix/orchestration/strategies/__init__.py",
)

#: 旧路径模块名 ↔ 新路径模块名（旧 import 已断言抛错）
MODULE_PAIRS = [
    ("agentix.agent.core.loop", "agentix.orchestration.orchestrator"),
    ("agentix.agent.executor.executor", "agentix.orchestration.executor"),
    ("agentix.agent.multiagent.bus", "agentix.orchestration.strategies.multi_agent.bus"),
    ("agentix.agent.multiagent.manager", "agentix.orchestration.strategies.multi_agent.manager"),
    ("agentix.agent.multiagent.peer", "agentix.orchestration.strategies.multi_agent.peer"),
    ("agentix.application.agents.lg_builder", "agentix.orchestration.langgraph.lg_builder"),
    ("agentix.application.agents.lg_states", "agentix.orchestration.langgraph.lg_states"),
    ("agentix.application.agents.lg_supervisor", "agentix.orchestration.langgraph.lg_supervisor"),
]

#: M2 旧路径孤儿 import（orchestration/** 禁引）
OLD_SIBLING_PREFIXES = (
    "agentix.agent.core.loop",
    "agentix.agent.executor",
    "agentix.agent.multiagent",
    "agentix.application.agents.lg_builder",
    "agentix.application.agents.lg_states",
    "agentix.application.agents.lg_supervisor",
)

LEGACY_EXTERNAL_OK = (
    # 允许的既有外部依赖列此备查：capabilities.tools / infrastructure.tools
    # 两簇已随 M6 删除（引用即 ImportError，等价于天然禁引）；lg_prompts 业务
    # prompt 留 application（§5 裁决），builders 保留上行引用——不以
    # OLD_SIBLING_PREFIXES 开头，天然不在禁引面内。
    "agentix.capabilities.tools.",
    "agentix.infrastructure.tools.",
    "agentix.application.agents.lg_prompts",
)


class TestTargetLayout:
    @pytest.mark.parametrize("rel", DELETED_OLD_PATHS)
    def test_old_path_deleted(self, rel: str) -> None:
        assert not (REPO / rel).exists(), f"M6 已完成但 M2 旧路径仍存在: {rel}"

    @pytest.mark.parametrize("rel", NEW_PKG_FILES)
    def test_new_package_init_exists(self, rel: str) -> None:
        assert (REPO / rel).is_file(), f"orchestration 包门面缺失: {rel}"

    @pytest.mark.parametrize(
        "rel",
        [
            "agentix/orchestration/orchestrator.py",
            "agentix/orchestration/executor.py",
            "agentix/orchestration/langgraph/lg_builder.py",
            "agentix/orchestration/langgraph/lg_states.py",
            "agentix/orchestration/langgraph/lg_supervisor.py",
            "agentix/orchestration/strategies/multi_agent/manager.py",
        ],
    )
    def test_new_home_holds_real_implementation(self, rel: str) -> None:
        """新家文件是真实实现（含定义），不是转发壳。"""
        text = (REPO / rel).read_text(encoding="utf-8")
        assert "_TARGET =" not in text, f"新家不允许是 shim: {rel}"

    @pytest.mark.parametrize("rel", IMPORT_FREE_INITS)
    def test_new_package_init_is_import_free(self, rel: str) -> None:
        """orchestration 系空门面零 import：防 lg_builder↔lg_supervisor 互引环
        与经 agent 包的回拉在 eager 汇总导出下炸半初始化。"""
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        imports = [
            n
            for n in tree.body
            if isinstance(n, (ast.Import, ast.ImportFrom))
        ]
        assert imports == [], f"{rel} 包门面出现 import（应保持零 import）"


class TestOldPathShimsDeleted:
    """M2 迁移期 8 个 sys.modules 别名式 shim + 1 个包门面：已全数删除。"""

    @pytest.mark.parametrize("old_rel,new_rel", sorted(MODULE_SHIMS.items()))
    def test_shim_file_deleted(self, old_rel: str, new_rel: str) -> None:
        assert not (REPO / old_rel).exists(), (
            f"M6 已完成但别名式 shim 仍存在: {old_rel}"
        )
        assert (REPO / new_rel).is_file(), f"规范位缺失: {new_rel}"

    @pytest.mark.parametrize("old,new", MODULE_PAIRS)
    def test_old_import_raises_new_importable(self, old: str, new: str) -> None:
        """旧路径 import 已断（迁移期 alias 语义随 shim 删除）；规范位可用。"""
        with pytest.raises(ImportError):
            __import__(old, fromlist=["*"])
        new_mod = __import__(new, fromlist=["*"])
        assert new_mod is not None


class TestAgentPackageLazyReexport:
    def test_agent_init_is_lazy(self) -> None:
        """agentix.agent.__init__ 必须惰性再导出 AgentLoop：

        orchestrator 先加载时其顶部 ``from agentix.agent.hooks import ...``
        会触发本包 __init__；若顶层 eager import 规范位会与回拉序纠缠
        （迁移期还会沿 shim 回拉半初始化的 orchestrator 炸 AttributeError——
        M6 删 shim 后该风险已消失，惰性仍保留为加载序纪律）。
        """
        tree = ast.parse((REPO / "agentix/agent/__init__.py").read_text(encoding="utf-8"))
        top_level_loop_imports = [
            n
            for n in tree.body
            if isinstance(n, ast.ImportFrom)
            and (n.module or "").startswith("agentix.agent.core")
        ]
        assert top_level_loop_imports == [], "agentix.agent.__init__ 不得顶层 import loop"

    def test_importing_agent_package_does_not_drag_orchestrator(self) -> None:
        code = (
            "import sys\n"
            "import agentix.agent\n"
            "assert 'agentix.orchestration.orchestrator' not in sys.modules, "
            "'import agentix.agent 提前拖入 orchestrator（应惰性）'\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.returncode == 0, proc.stderr

    def test_agent_reexport_identity(self) -> None:
        import agentix.agent as agent_pkg
        import agentix.orchestration.orchestrator as orch

        assert agent_pkg.AgentLoop is orch.AgentLoop
        assert agent_pkg.LoopResult is orch.LoopResult


class TestSymbolIdentity:
    def test_loop_symbols_gone_from_old_path(self) -> None:
        with pytest.raises(ImportError):
            __import__("agentix.agent.core.loop", fromlist=["*"])
        import agentix.orchestration.orchestrator as orch

        for name in ("AgentLoop", "LoopResult", "StepExecutorPort"):
            assert hasattr(orch, name), f"规范位 orchestrator 缺 {name}"

    def test_executor_package_gone(self) -> None:
        with pytest.raises(ImportError):
            __import__("agentix.agent.executor", fromlist=["*"])
        import agentix.orchestration.executor as ex

        assert hasattr(ex, "MappingStepExecutor") and hasattr(ex, "ToolCallable")

    def test_multiagent_package_gone(self) -> None:
        with pytest.raises(ImportError):
            __import__("agentix.agent.multiagent", fromlist=["*"])
        import agentix.orchestration.strategies.multi_agent as home

        assert list(home.__all__), "规范位 multi_agent 包须有导出面"

    def test_multiagent_old_init_deleted(self) -> None:
        assert not (
            REPO / "agentix" / "agent" / "multiagent" / "__init__.py"
        ).exists()


class TestMonkeypatchRoundtrip:
    def test_patch_via_new_path_works_and_old_is_gone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """phase19/p12 回归契约（M6 口径）：patch 只能落在规范位上；
        迁移期「经旧路径 setattr 传导」的别名语义随 shim 删除，旧路径已断。"""
        import agentix.orchestration.langgraph.lg_supervisor as real

        with pytest.raises(ImportError):
            __import__("agentix.application.agents.lg_supervisor", fromlist=["*"])

        sentinel = object()
        monkeypatch.setattr(real, "M71_SENTINEL", sentinel, raising=False)
        assert real.M71_SENTINEL is sentinel

    def test_route_handlers_lives_on_real_module(self) -> None:
        import agentix.orchestration.langgraph.lg_supervisor as real

        assert real.ROUTE_HANDLERS


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

    def test_orchestration_never_imports_m2_old_paths(self) -> None:
        offenders: List[str] = []
        for f in sorted((REPO / "agentix/orchestration").rglob("*.py")):
            for name in self._imported_names(f):
                if name.startswith(OLD_SIBLING_PREFIXES):
                    offenders.append(
                        f"{f.relative_to(REPO).as_posix()} -> {name}"
                    )
        assert offenders == [], (
            "orchestration/** 回引 M2 旧路径（迁移必须切断旧路径依赖）: "
            f"{offenders}"
        )

    def test_lg_builders_keep_business_prompt_reference(self) -> None:
        """lg_prompts 业务 prompt 留 application（§5 裁决）——builders 保留上行引用。"""
        text = (
            REPO / "agentix/orchestration/langgraph/lg_builder.py"
        ).read_text(encoding="utf-8")
        assert "agentix.application.agents.lg_prompts" in text


class TestLgPromptsStays:
    def test_lg_prompts_is_real_file_not_shim(self) -> None:
        rel = "agentix/application/agents/lg_prompts.py"
        text = (REPO / rel).read_text(encoding="utf-8")
        assert "_TARGET" not in text, "业务 prompt 文件不应是 shim"
        assert "ROUTER_SYSTEM_PROMPT" in text

    def test_lg_prompts_importable(self) -> None:
        from agentix.application.agents import lg_prompts

        assert hasattr(lg_prompts, "ROUTER_SYSTEM_PROMPT")
        assert not hasattr(lg_prompts, "_TARGET")


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

    def test_orchestrator_loaded_first(self) -> None:
        """危险序：orchestrator 先于 agent 包加载（回拉环的触发序）。"""
        self._run(
            "import agentix.orchestration.orchestrator as O\n"
            "import agentix.agent as A\n"
            "assert A.AgentLoop is O.AgentLoop\n"
            "assert A.LoopResult is O.LoopResult\n"
        )

    def test_agent_package_loaded_first(self) -> None:
        self._run(
            "import agentix.agent as A\n"
            "from agentix.agent import AgentLoop, LoopResult\n"
            "import agentix.orchestration.orchestrator as O\n"
            "assert AgentLoop is O.AgentLoop and LoopResult is O.LoopResult\n"
        )

    def test_supervisor_new_path_works_old_path_gone(self) -> None:
        self._run(
            "import importlib\n"
            "try:\n"
            "    importlib.import_module('agentix.application.agents.lg_supervisor')\n"
            "except ImportError:\n"
            "    pass\n"
            "else:\n"
            "    raise SystemExit('旧路径仍可导入')\n"
            "from agentix.orchestration.langgraph import lg_supervisor as S2\n"
            "assert S2.supervisor_dispatch\n"
        )

    def test_multiagent_canonical_import(self) -> None:
        self._run(
            "from agentix.orchestration.strategies.multi_agent import Manager, InProcessBus\n"
            "assert Manager and InProcessBus\n"
        )
