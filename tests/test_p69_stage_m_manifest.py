"""p69 Stage M · M0：契约拆分落位断言 + 逐文件迁移清单冻结（蓝图 §4 L117 / §5 L131-151）。

M0 交付两样东西，本测试即其契约面：

1. **契约拆分落位**——``domain/agent``、``domain/runtime``、``domain/tool`` 三个
   纯契约子包存在且为规范位；旧路径在 M1–M5 迁移期为 re-export shim（对象同一
   ``is``，不是第二份拷贝），**M6（2026-10-03）清尾后已全部删除**——断言方向
   已翻转：旧路径 ``ModuleNotFoundError``、规范位单一事实来源。
2. **逐文件迁移清单冻结**——蓝图 §5 的骨架在此展开为结构化 manifest，后续
   M1–M6 每批只允许「按清单搬家」，不允许清单外私迁。每批完成后把 stage
   加入 ``COMPLETED_STAGES``（与 README 计数同纪律），断言方向随之翻转：
   迁移批 = 目标存在（源可留 shim 或已删）、删除批 = 目标不存在。
   M6 已登记——``COMPLETED_STAGES`` 含 ``{"M0"…"M6"}``。
"""

from __future__ import annotations

import glob as globmod
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# ── M0：契约拆分规范位（新家）────────────────────────────────────────────
CANONICAL = [
    # domain/agent：Task/Plan/Observation 等类型 + 七端口（拆自 agent_types / agent_core）
    "agentix/domain/agent/types.py",
    "agentix/domain/agent/ports.py",
    "agentix/domain/agent/__init__.py",
    # domain/runtime：Run/Step/Budget/Lifecycle 契约
    "agentix/domain/runtime/runs.py",
    "agentix/domain/runtime/steps.py",
    "agentix/domain/runtime/budget.py",
    "agentix/domain/runtime/lifecycle.py",
    "agentix/domain/runtime/__init__.py",
    # M0 契约期即为规范位的两处（迁移期为 shim，M6 后恢复单一来源）
    "agentix/domain/status.py",
    "agentix/runtime/state.py",
    # domain/tool：ToolSpec / Risk / Permission 契约
    "agentix/domain/tool/spec.py",
    "agentix/domain/tool/risk.py",
    "agentix/domain/tool/permission.py",
    "agentix/domain/tool/__init__.py",
]

#: 迁移期为 re-export shim 的旧路径 + M6 三簇目录——**M6 清尾后全部删除**，
#: 一律 ``ModuleNotFoundError``（p75 收口断言零残留）。
SHIM_PATHS_DELETED_M6 = [
    "agentix/domain/agent_types.py",
    "agentix/domain/ports/agent_core.py",
    "agentix/application/services/control_plane.py",
    "agentix/capabilities/tools/definitions.py",
    "agentix/capabilities/tools/human_review.py",
    "agentix/application/services/hitl_bridge.py",
    "agentix/capabilities/tools",
    "agentix/mcp",
    "agentix/infrastructure/tools",
]

# ── M1–M6 逐文件迁移清单（§5 骨架展开；M0 冻结）─────────────────────────
# 条目 = (stage, source, target, mode)
#   mode = "迁移" : 搬家 + 旧路径 re-export shim（M6 清尾）
#   mode = "改造" : 物理位置不动，只剥离/收敛行为（M5）
#   mode = "删除" : 实现归并他处后整目录删除（M6，或先迁引用后删）
# target 为 "" 仅允许出现在 "删除" 条目。
MIGRATION_MANIFEST: list[tuple[str, str, str, str]] = [
    # M1：capabilities/tools → 顶层 tools/（gateway/registry/adapters）
    ("M1", "agentix/capabilities/tools", "agentix/tools", "迁移"),
    # M2：编排件 → orchestration/
    ("M2", "agentix/agent/core/loop.py", "agentix/orchestration/orchestrator.py", "迁移"),
    ("M2", "agentix/agent/executor", "agentix/orchestration/executor.py", "迁移"),
    ("M2", "agentix/application/agents/lg_*", "agentix/orchestration/langgraph", "迁移"),
    ("M2", "agentix/agent/multiagent", "agentix/orchestration/strategies/multi_agent", "迁移"),
    # M3：策略件 → policies/
    ("M3", "agentix/agent/planner", "agentix/policies/planner", "迁移"),
    ("M3", "agentix/agent/replanner", "agentix/policies/planner", "迁移"),
    ("M3", "agentix/agent/verifier", "agentix/policies/verifier", "迁移"),
    ("M3", "agentix/agent/hitl", "agentix/policies/hitl", "迁移"),
    ("M3", "agentix/application/services/hitl_bridge.py", "agentix/policies/hitl", "迁移"),
    ("M3", "agentix/capabilities/tools/guardrails.py", "agentix/policies/guardrails", "迁移"),
    # M4：ControlPlane → runtime/（Run registry / cancellation 归位）
    ("M4", "agentix/application/services/control_plane.py", "agentix/runtime/control_plane.py", "迁移"),
    # M5：retrieval 收编——执行链剥离（chat_agent_runner 改造）+ 纯策略件迁共享域
    ("M5", "agentix/retrieval/usecases/chat_agent_runner.py", "agentix/retrieval/usecases/chat_agent_runner.py", "改造"),
    ("M5", "agentix/retrieval/domain/policies/legal_effectiveness.py", "agentix/domain/policies/legal_effectiveness.py", "迁移"),
    ("M5", "agentix/retrieval/domain/services/grounding_verify.py", "agentix/domain/policies/grounding_verify.py", "迁移"),
    # M6：删三簇（实现先归并 tools/ 与 capabilities/mcp，再删；第三簇
    # capabilities/tools 的删除覆盖在上方 M1 条目的 source 上 + p75 收口断言）
    ("M6", "agentix/infrastructure/tools", "", "删除"),
    ("M6", "agentix/mcp", "", "删除"),
]

#: 已完成的 Stage M 批次（每批落地后在此登记；M0 见下方契约断言）。
#: M6（2026-10-03）：三簇 shim + 全部旧路径清尾完成——Stage M 全批次收官。
COMPLETED_STAGES = frozenset({"M0", "M1", "M2", "M3", "M4", "M5", "M6"})

VALID_MODES = {"迁移", "改造", "删除"}


def _exists(rel: str) -> bool:
    """存在性检查（支持 ``*`` 通配，如 lg_*）。"""
    if "*" in rel:
        return bool(globmod.glob(str(REPO / rel)))
    return (REPO / rel).exists()


class TestM0ContractSplit:
    """M0 契约拆分：规范位存在 + 旧路径已删（单一事实来源）。"""

    @pytest.mark.parametrize("rel", CANONICAL)
    def test_canonical_module_exists(self, rel: str):
        assert (REPO / rel).is_file(), f"M0 规范位缺失: {rel}"

    @pytest.mark.parametrize("rel", SHIM_PATHS_DELETED_M6)
    def test_old_shim_path_deleted(self, rel: str):
        assert not (REPO / rel).exists(), (
            f"M6 已完成但旧路径 shim 仍存在（应删除）: {rel}"
        )

    def test_agent_types_shim_is_gone(self):
        import importlib

        with pytest.raises(ImportError):
            importlib.import_module("agentix.domain.agent_types")

    def test_agent_core_shim_is_gone(self):
        import importlib

        with pytest.raises(ImportError):
            importlib.import_module("agentix.domain.ports.agent_core")

    @pytest.mark.parametrize(
        "shed_mod, names, canon_mod",
        [
            # 迁移期 shim 名——M6 拆除后旧模块上已不存在，规范位保有
            ("agentix.domain.status", ["ExitStatus", "ExitReason"],
             "agentix.domain.runtime.lifecycle"),
            ("agentix.runtime.state", ["StepStatus"], "agentix.domain.runtime.steps"),
            ("agentix.runtime.steps", ["StepType"], "agentix.domain.runtime.steps"),
        ],
    )
    def test_shed_names_gone_from_old_module(
        self, shed_mod: str, names: list[str], canon_mod: str
    ):
        import importlib

        old = importlib.import_module(shed_mod)
        canon = importlib.import_module(canon_mod)
        for name in names:
            assert not hasattr(old, name), (
                f"{shed_mod}.{name} 仍存在——M6 已把该名收敛到 {canon}"
            )
            assert hasattr(canon, name), f"规范位 {canon_mod}.{name} 缺失"

    @pytest.mark.parametrize(
        "live_mod, names",
        [
            # M6 后仍以规范位再导出存活的契约名（单一对象）
            ("agentix.runtime.state", ["BudgetState"]),
            ("agentix.runtime.budget", ["Budget", "BudgetExceededError", "BudgetState"]),
        ],
    )
    def test_surviving_names_are_canonical_objects(self, live_mod: str, names: list[str]):
        import importlib

        canon_map = {
            "BudgetState": "agentix.domain.runtime.budget",
            "Budget": "agentix.domain.runtime.budget",
            "BudgetExceededError": "agentix.domain.runtime.budget",
        }
        old = importlib.import_module(live_mod)
        for name in names:
            new = importlib.import_module(canon_map[name])
            assert getattr(old, name) is getattr(new, name), f"{live_mod}.{name} 不是规范位同一对象"

    @pytest.mark.parametrize(
        "old_mod, names",
        [
            ("agentix.application.services.control_plane", ["RunState", "TERMINAL_STATES"]),
            ("agentix.capabilities.tools.definitions", ["ToolSpec", "RISK_LEVELS", "FORBIDDEN_RISK_LEVEL"]),
            ("agentix.capabilities.tools.human_review", ["RiskLevel"]),
            ("agentix.application.services.hitl_bridge", ["PermissionMode"]),
        ],
    )
    def test_deleted_tool_shims_raise(self, old_mod: str, names: list[str]):
        import importlib

        with pytest.raises(ImportError):
            importlib.import_module(old_mod)


class TestMigrationManifestFrozen:
    """M1–M6 逐文件清单的结构校验 + 按阶段的存在性翻转。"""

    def test_manifest_is_nonempty_and_staged(self):
        assert MIGRATION_MANIFEST, "清单不得为空（M0 冻结）"
        for stage, source, target, mode in MIGRATION_MANIFEST:
            assert stage in {"M1", "M2", "M3", "M4", "M5", "M6"}, f"未知阶段: {stage}"
            assert mode in VALID_MODES, f"{source}: 未知 mode {mode}"
            assert source.startswith("agentix/"), f"source 必须在 agentix/ 下: {source}"
            if mode == "删除":
                assert target == "", f"删除条目 target 必须为空: {source}"
            else:
                assert target.startswith("agentix/"), f"target 必须在 agentix/ 下: {source}"
                if mode == "改造":
                    assert source == target, f"改造条目 source==target: {source}"
            if mode == "迁移":
                assert source != target, f"迁移条目 source≠target: {source}"

    def test_no_duplicate_source(self):
        sources = [s for _, s, _, _ in MIGRATION_MANIFEST]
        assert len(sources) == len(set(sources)), "source 条目重复"

    def test_completed_stages_are_valid(self):
        stages = {s for s, _, _, _ in MIGRATION_MANIFEST}
        assert COMPLETED_STAGES - {"M0"} <= stages, "COMPLETED_STAGES 含清单外 stage"

    @pytest.mark.parametrize("entry_index", range(len(MIGRATION_MANIFEST)))
    def test_pending_source_exists_completed_target_exists(self, entry_index: int):
        stage, source, target, mode = MIGRATION_MANIFEST[entry_index]
        if stage in COMPLETED_STAGES:
            if mode == "删除":
                assert not _exists(source), f"{stage} 已完成但源仍存在: {source}"
            else:
                assert _exists(target), f"{stage} 已完成但目标缺失: {target}"
        else:
            assert _exists(source), f"{stage} 未登记完成但源已消失（应先登记再删）: {source}"

    def test_m0_its_own_stage(self):
        # M0 不进逐文件清单（契约拆分由 TestM0ContractSplit 覆盖）
        assert all(s != "M0" for s, _, _, _ in MIGRATION_MANIFEST)
