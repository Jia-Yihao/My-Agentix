"""Phase 2 — Agent Core Loop 验收测试。

覆盖：happy path、预算耗尽、验证失败终态、工具异常归一、声明式多步、
context/plan 失败兜底、LangGraph 仅限 graph/ 子包的静态守卫。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agentix.agent import AgentLoop, LoopResult
from agentix.agent.context import SimpleContextBuilder
from agentix.orchestration.executor import MappingStepExecutor
from agentix.policies.planner import DeclaredStepsPlanner, SingleStepPlanner
from agentix.policies.verifier import StructuralVerifier
from agentix.domain.agent.types import TaskSpec

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = PROJECT_ROOT / "agentix" / "agent"


class _AlwaysPassVerifier:
    async def verify(self, task, plan, observations):
        from agentix.domain.agent.types import VerificationResult

        return VerificationResult(passed=True, reason="always")


class _AlwaysFailVerifier:
    async def verify(self, task, plan, observations):
        from agentix.domain.agent.types import VerificationResult

        return VerificationResult(passed=False, reason="always_fail")


class _FlakyContext:
    async def build(self, task, *, trajectory=(), budget_tokens=0):
        raise RuntimeError("ctx down")


class _FlakyPlanner:
    async def plan(self, task, *, context=None):
        raise RuntimeError("plan down")


def _ok_tools():
    return {
        "act": lambda **kw: "done-echo",
        "search": lambda **kw: "hit-doc-1",
        "summarize": lambda **kw: "summary-ok",
    }


@pytest.mark.asyncio
async def test_happy_path_single_step():
    loop = AgentLoop(
        planner=SingleStepPlanner(),
        context=SimpleContextBuilder(),
        verifier=StructuralVerifier(),
        executor=MappingStepExecutor(_ok_tools()),
        max_steps=5,
    )
    result = await loop.run(TaskSpec(goal="say done-echo", task_id="t1"))
    assert isinstance(result, LoopResult)
    assert result.passed is True
    assert result.terminal == "completed"
    assert result.final == "done-echo"
    assert result.steps_used == 1
    assert len(result.observations) == 1


@pytest.mark.asyncio
async def test_budget_exceeded_terminal():
    task = TaskSpec(
        goal="loop",
        task_id="t2",
        constraints={
            "steps": [
                {"id": "a", "action": "act", "tool": "act"},
                {"id": "b", "action": "act", "tool": "act"},
                {"id": "c", "action": "act", "tool": "act"},
            ]
        },
    )
    loop = AgentLoop(
        planner=DeclaredStepsPlanner(),
        context=SimpleContextBuilder(),
        verifier=_AlwaysPassVerifier(),
        executor=MappingStepExecutor(_ok_tools()),
        max_steps=2,
    )
    result = await loop.run(task)
    assert result.terminal == "budget_exceeded"
    assert result.passed is False
    assert result.steps_used == 2


@pytest.mark.asyncio
async def test_verify_fail_terminal():
    loop = AgentLoop(
        planner=SingleStepPlanner(),
        context=SimpleContextBuilder(),
        verifier=_AlwaysFailVerifier(),
        executor=MappingStepExecutor(_ok_tools()),
        max_steps=5,
    )
    result = await loop.run(TaskSpec(goal="x", task_id="t3"))
    assert result.terminal == "verify_failed"
    assert result.passed is False


@pytest.mark.asyncio
async def test_tool_missing_marks_observation_failed():
    loop = AgentLoop(
        planner=DeclaredStepsPlanner(),
        context=SimpleContextBuilder(),
        verifier=_AlwaysPassVerifier(),
        executor=MappingStepExecutor({}),  # no tools
        max_steps=5,
    )
    task = TaskSpec(
        goal="g",
        task_id="t4",
        constraints={"steps": [{"id": "a", "action": "search", "tool": "search"}]},
    )
    result = await loop.run(task)
    assert result.steps_used == 1
    assert result.observations[0].ok is False
    assert result.observations[0].error_code == "tool_not_found"


@pytest.mark.asyncio
async def test_tool_exception_becomes_failed_observation_not_crash():
    def boom(**kw):
        raise ValueError("bad tool")

    loop = AgentLoop(
        planner=SingleStepPlanner(),
        context=SimpleContextBuilder(),
        verifier=_AlwaysPassVerifier(),
        executor=MappingStepExecutor({"act": boom}),
        max_steps=5,
    )
    result = await loop.run(TaskSpec(goal="g", task_id="t5"))
    assert result.terminal in {"completed", "verify_failed"}
    assert result.observations[0].ok is False
    assert "ValueError" in result.observations[0].content


@pytest.mark.asyncio
async def test_context_failure_is_terminal_failed():
    loop = AgentLoop(
        planner=SingleStepPlanner(),
        context=_FlakyContext(),
        verifier=_AlwaysPassVerifier(),
        executor=MappingStepExecutor(_ok_tools()),
    )
    result = await loop.run(TaskSpec(goal="g", task_id="t6"))
    assert result.terminal == "failed"
    assert "context_build_failed" in result.reason


@pytest.mark.asyncio
async def test_plan_failure_is_terminal_failed():
    loop = AgentLoop(
        planner=_FlakyPlanner(),
        context=SimpleContextBuilder(),
        verifier=_AlwaysPassVerifier(),
        executor=MappingStepExecutor(_ok_tools()),
    )
    result = await loop.run(TaskSpec(goal="g", task_id="t7"))
    assert result.terminal == "failed"
    assert "plan_failed" in result.reason


@pytest.mark.asyncio
async def test_declared_multi_step_order():
    order: list[str] = []

    async def track(**kw):
        step = kw["step"]
        order.append(step.id)
        return f"ok-{step.id}"

    task = TaskSpec(
        goal="g",
        task_id="t8",
        constraints={
            "steps": [
                {"id": "a", "action": "act", "tool": "act"},
                {"id": "b", "action": "act", "tool": "act", "depends_on": ["a"]},
                {"id": "c", "action": "act", "tool": "act", "depends_on": ["b"]},
            ]
        },
    )
    loop = AgentLoop(
        planner=DeclaredStepsPlanner(),
        context=SimpleContextBuilder(),
        verifier=_AlwaysPassVerifier(),
        executor=MappingStepExecutor({"act": track}),
        max_steps=10,
    )
    result = await loop.run(task)
    assert order == ["a", "b", "c"]
    assert result.terminal == "completed"
    assert result.final == "ok-c"


@pytest.mark.asyncio
async def test_structural_verifier_success_criteria():
    loop = AgentLoop(
        planner=SingleStepPlanner(),
        context=SimpleContextBuilder(),
        verifier=StructuralVerifier(),
        executor=MappingStepExecutor({"act": lambda **kw: "contains KEYWORD here"}),
        max_steps=5,
    )
    result = await loop.run(
        TaskSpec(goal="g", task_id="t9", success_criteria=("KEYWORD",))
    )
    assert result.passed is True

    loop2 = AgentLoop(
        planner=SingleStepPlanner(),
        context=SimpleContextBuilder(),
        verifier=StructuralVerifier(),
        executor=MappingStepExecutor({"act": lambda **kw: "no match"}),
        max_steps=5,
    )
    result2 = await loop2.run(
        TaskSpec(goal="g", task_id="t10", success_criteria=("KEYWORD",))
    )
    assert result2.passed is False
    assert result2.terminal == "verify_failed"


class TestLangGraphIsolation:
    def test_langgraph_only_under_graph_subdir(self):
        """agent 包内 langgraph import 仅允许 graph/（Phase 2 静态约束）。"""
        offenders = {}
        for path in AGENT_DIR.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mods = [node.module]
                for m in mods:
                    if m.startswith("langgraph"):
                        rel = path.relative_to(AGENT_DIR).as_posix()
                        if not rel.startswith("graph/"):
                            offenders.setdefault(rel, []).append(m)
        assert offenders == {}, f"langgraph 出现在 graph/ 之外: {offenders}"

    def test_agent_has_no_infrastructure_import(self):
        offenders = {}
        for path in AGENT_DIR.rglob("*.py"):
            src = path.read_text(encoding="utf-8")
            if "agentix.infrastructure" in src or "agentix.interfaces" in src:
                offenders[path.relative_to(AGENT_DIR).as_posix()] = True
        assert offenders == {}, f"agent 依赖上层: {offenders}"
