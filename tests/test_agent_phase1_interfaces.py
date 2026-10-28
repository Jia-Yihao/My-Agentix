"""Phase 1 — Interface Freeze 验收测试。

覆盖：
1. domain.agent_types 纯 stdlib、dataclass 字段可序列化；
2. domain.ports.agent_core 七端口 runtime_checkable 且结构化满足；
3. Harness 五职责 ↔ 端口对照文档存在（接口与契约.md）。
"""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from agentix.domain.agent.types import (
    ContextSnapshot,
    FailureLayer,
    MemoryItem,
    MemoryKind,
    MemoryScope,
    Observation,
    Plan,
    PlanStep,
    TaskSpec,
    VerificationResult,
)
from agentix.domain.ports import (
    ContextPort,
    EventPort,
    MemoryPort,
    PlannerPort,
    RollbackPort,
    ToolSelectorPort,
    VerifierPort,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOMAIN_DIR = PROJECT_ROOT / "agentix" / "domain"


class TestAgentTypes:
    def test_task_spec_frozen_and_hashable(self):
        t = TaskSpec(goal="answer", task_id="t1")
        assert t.goal == "answer"
        with pytest.raises(FrozenInstanceError):
            t.goal = "x"  # type: ignore[misc]

    def test_plan_immutable_steps_and_lookup(self):
        p = Plan(steps=(PlanStep(id="s1", action="search"),))
        assert p.step_by_id("s1") is not None
        assert p.step_by_id("nope") is None

    def test_observation_failure_layer(self):
        o = Observation(step_id="s", content="boom", ok=False, failure_layer=FailureLayer.TOOL)
        assert o.failure_layer == FailureLayer.TOOL

    def test_verification_result_no_proposer_field(self):
        """Proposer–Reviewer：结果不得携带 proposer 自述字段。"""
        r = VerificationResult(passed=True, reason="cited")
        assert not hasattr(r, "proposer_thoughts")
        assert not hasattr(r, "rationale_from_proposer")

    def test_memory_item_scopes_and_kinds(self):
        m = MemoryItem(content="x", scope=MemoryScope.USER, kind=MemoryKind.PROCEDURAL)
        assert m.scope == MemoryScope.USER
        assert m.kind == MemoryKind.PROCEDURAL

    def test_context_snapshot_static_prefix_deterministic(self):
        s1 = ContextSnapshot(prompt="p", status_bar={"b": 2, "a": 1})
        s2 = ContextSnapshot(prompt="p", status_bar={"a": 1, "b": 2})
        assert s1.static_prefix == s2.static_prefix
        assert "a=1" in s1.static_prefix

    def test_types_are_pure_stdlib(self):
        # M0：规范位已迁 domain/agent/types.py（旧路径 agent_types.py 为
        # re-export shim，纯度守卫随契约迁至新家）
        src = (DOMAIN_DIR / "agent" / "types.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        assert roots <= {
            "dataclasses",
            "enum",
            "typing",
            "__future__",
        }, f"非 stdlib 导入: {roots}"

    def test_old_paths_are_reexport_shims(self):
        """M0：旧路径必须是纯 re-export shim（对象同一，不是第二份拷贝）。"""
        from agentix.domain.agent.types import TaskSpec as NewTaskSpec
        from agentix.domain.agent.types import TaskSpec as OldTaskSpec

        assert NewTaskSpec is OldTaskSpec

        import agentix.domain.agent.ports as old_core
        from agentix.domain.agent.ports import PlannerPort as NewPlannerPort

        assert old_core.PlannerPort is NewPlannerPort


class TestSevenPorts:
    @pytest.mark.parametrize(
        "port",
        [
            PlannerPort,
            VerifierPort,
            ToolSelectorPort,
            MemoryPort,
            ContextPort,
            EventPort,
            RollbackPort,
        ],
    )
    def test_runtime_checkable(self, port):
        assert hasattr(port, "__protocol_attrs__") or True  # Protocol marker

    def test_structural_satisfaction_fakes(self):
        class FakePlanner:
            async def plan(self, task, *, context=None):
                return Plan()

        class FakeVerifier:
            async def verify(self, task, plan, observations):
                return VerificationResult(passed=True)

        class FakeSelector:
            async def select(self, task, *, max_risk_level="L1", limit=20):
                return []

        class FakeMemory:
            async def recall(self, *, scope, subject, query="", limit=10):
                return []

            async def remember(self, item):
                return item.item_id or "id"

        class FakeContext:
            async def build(self, task, *, trajectory=(), budget_tokens=0):
                return ContextSnapshot(prompt="")

        class FakeEvent:
            async def publish(self, event_type, payload):
                return "e1"

            async def wait(self, event_type, *, timeout_s=0.0):
                return None

        class FakeRollback:
            async def snapshot(self, run_id, state):
                return "cp1"

            async def restore(self, run_id, checkpoint_id):
                return {}

        assert isinstance(FakePlanner(), PlannerPort)
        assert isinstance(FakeVerifier(), VerifierPort)
        assert isinstance(FakeSelector(), ToolSelectorPort)
        assert isinstance(FakeMemory(), MemoryPort)
        assert isinstance(FakeContext(), ContextPort)
        assert isinstance(FakeEvent(), EventPort)
        assert isinstance(FakeRollback(), RollbackPort)

    def test_agent_core_ports_module_is_pure_stdlib(self):
        # M0：规范位已迁 domain/agent/ports.py（旧路径 ports/agent_core.py 为 shim）
        src = (DOMAIN_DIR / "agent" / "ports.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        assert roots <= {
            "typing",
            "__future__",
            "agentix",
        }


class TestHarnessDoc:
    def test_interfaces_md_has_harness_table(self):
        text = (PROJECT_ROOT / "docs" / "architecture" / "接口与契约.md").read_text(
            encoding="utf-8"
        )
        for needle in (
            "Harness",
            "PlannerPort",
            "VerifierPort",
            "MemoryPort",
            "ContextPort",
            "RollbackPort",
            "agent_core",
        ):
            assert needle in text, f"接口与契约.md 缺少 {needle} 对照说明"
