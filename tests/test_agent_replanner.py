"""Phase 3 — Replanning + 四层故障注入验收。

书 §5.7：API / 工具 / 上下文 / 控制流 四层故障；
断言：never hang、never duplicate side effect、always terminal。
"""

from __future__ import annotations

import asyncio

import pytest

from agentix.agent import AgentLoop
from agentix.agent.context import SimpleContextBuilder
from agentix.orchestration.executor import MappingStepExecutor
from agentix.policies.planner import DeclaredStepsPlanner, SingleStepPlanner
from agentix.policies.planner import RetryReplanner, failure_key
from agentix.domain.agent.types import FailureLayer, Observation, TaskSpec


class _PassVerifier:
    async def verify(self, task, plan, observations):
        from agentix.domain.agent.types import VerificationResult

        return VerificationResult(passed=True, reason="ok")


class _FailThenPassTool:
    """前 N 次抛错，之后成功（验证重规划有效）。"""

    def __init__(self, fail_times: int = 1, exc: Exception | None = None):
        self.fail_times = fail_times
        self.calls = 0
        self.exc = exc or RuntimeError("transient tool error")

    def __call__(self, **kw):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return "recovered"


@pytest.mark.asyncio
async def test_replan_after_tool_failure_recovers():
    tool = _FailThenPassTool(fail_times=1)
    rp = RetryReplanner(max_replans=3, same_failure_threshold=3)
    loop = AgentLoop(
        planner=SingleStepPlanner(),
        context=SimpleContextBuilder(),
        verifier=_PassVerifier(),
        executor=MappingStepExecutor({"act": tool}),
        max_steps=10,
        max_replans=3,
        replanner=rp,
    )
    result = await loop.run(TaskSpec(goal="g", task_id="r1"))
    assert result.replans >= 1
    assert result.terminal == "completed"
    assert tool.calls >= 2
    # 每个 step_id 只执行一次（无重复副作用）
    ids = [o.step_id for o in result.observations]
    assert len(ids) == len(set(ids))


@pytest.mark.asyncio
async def test_same_failure_burns_out_no_infinite_replan():
    tool = _FailThenPassTool(fail_times=99)  # 永远失败
    rp = RetryReplanner(max_replans=3, same_failure_threshold=2)
    loop = AgentLoop(
        planner=SingleStepPlanner(),
        context=SimpleContextBuilder(),
        verifier=_PassVerifier(),
        executor=MappingStepExecutor({"act": tool}),
        max_steps=20,
        max_replans=3,
        replanner=rp,
    )
    result = await loop.run(TaskSpec(goal="g", task_id="r2"))
    # 同因连续失败 → 熔断，replans 受限，终态可达
    assert result.terminal in {"completed", "verify_failed", "budget_exceeded", "failed"}
    assert result.replans <= 3
    assert tool.calls <= 10  # 不无限调用


@pytest.mark.asyncio
async def test_max_replans_cap():
    tool = _FailThenPassTool(fail_times=99)
    rp = RetryReplanner(max_replans=1, same_failure_threshold=10)
    loop = AgentLoop(
        planner=SingleStepPlanner(),
        context=SimpleContextBuilder(),
        verifier=_PassVerifier(),
        executor=MappingStepExecutor({"act": tool}),
        max_steps=15,
        max_replans=1,
        replanner=rp,
    )
    result = await loop.run(TaskSpec(goal="g", task_id="r3"))
    assert result.replans <= 1


class TestFourLayerFaults:
    """每层至少 1 个注入用例；全部必须到终态。"""

    @pytest.mark.asyncio
    async def test_api_layer_exception_classified(self):
        # 模拟上游 LLM/限流异常经工具边界冒泡
        def rate_limited(**kw):
            raise RuntimeError("rate limit 429 from llm")

        loop = AgentLoop(
            planner=SingleStepPlanner(),
            context=SimpleContextBuilder(),
            verifier=_PassVerifier(),
            executor=MappingStepExecutor({"act": rate_limited}),
            max_steps=5,
        )
        result = await loop.run(TaskSpec(goal="g", task_id="f_api"))
        assert result.terminal in {"completed", "verify_failed", "budget_exceeded", "failed"}
        assert result.observations[0].failure_layer == FailureLayer.API

    @pytest.mark.asyncio
    async def test_tool_layer_exception_classified(self):
        def tool_boom(**kw):
            raise KeyError("missing_row")

        loop = AgentLoop(
            planner=SingleStepPlanner(),
            context=SimpleContextBuilder(),
            verifier=_PassVerifier(),
            executor=MappingStepExecutor({"act": tool_boom}),
            max_steps=5,
        )
        result = await loop.run(TaskSpec(goal="g", task_id="f_tool"))
        assert result.observations[0].failure_layer == FailureLayer.TOOL

    @pytest.mark.asyncio
    async def test_context_layer_exception_classified(self):
        def overflow(**kw):
            raise RuntimeError("context overflow: prompt too long")

        loop = AgentLoop(
            planner=SingleStepPlanner(),
            context=SimpleContextBuilder(),
            verifier=_PassVerifier(),
            executor=MappingStepExecutor({"act": overflow}),
            max_steps=5,
        )
        result = await loop.run(TaskSpec(goal="g", task_id="f_ctx"))
        assert result.observations[0].failure_layer == FailureLayer.CONTEXT

    @pytest.mark.asyncio
    async def test_control_flow_budget_terminal(self):
        task = TaskSpec(
            goal="g",
            task_id="f_ctl",
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
            verifier=_PassVerifier(),
            executor=MappingStepExecutor({"act": lambda **kw: "x"}),
            max_steps=1,
        )
        result = await loop.run(task)
        assert result.terminal == "budget_exceeded"

    @pytest.mark.asyncio
    async def test_context_build_failure_terminal(self):
        class BadCtx:
            async def build(self, task, *, trajectory=(), budget_tokens=0):
                raise MemoryError("context window exhausted")

        loop = AgentLoop(
            planner=SingleStepPlanner(),
            context=BadCtx(),
            verifier=_PassVerifier(),
            executor=MappingStepExecutor({"act": lambda **kw: "x"}),
            max_steps=5,
        )
        result = await loop.run(TaskSpec(goal="g", task_id="f_ctx2"))
        assert result.terminal == "failed"
        assert "context_build_failed" in result.reason


@pytest.mark.asyncio
async def test_never_hang_always_terminal_under_random_failures():
    """随机故障下 50 次运行全部到达终态且不超时。"""
    class Flaky:
        def __init__(self):
            self.n = 0

        def __call__(self, **kw):
            self.n += 1
            if self.n % 3 == 0:
                raise RuntimeError("transient")
            return f"ok-{self.n}"

    for i in range(50):
        loop = AgentLoop(
            planner=SingleStepPlanner(),
            context=SimpleContextBuilder(),
            verifier=_PassVerifier(),
            executor=MappingStepExecutor({"act": Flaky()}),
            max_steps=8,
            max_replans=2,
            replanner=RetryReplanner(max_replans=2, same_failure_threshold=3),
        )
        result = await asyncio.wait_for(
            loop.run(TaskSpec(goal="g", task_id=f"h{i}")),
            timeout=2.0,
        )
        assert result.terminal in {
            "completed",
            "verify_failed",
            "budget_exceeded",
            "failed",
        }


def test_failure_key_stable():
    o1 = Observation(step_id="s", content="timeout after 30s", ok=False, error_code="tool_exec_failed")
    o2 = Observation(step_id="s", content="timeout after 30s", ok=False, error_code="tool_exec_failed")
    assert failure_key(o1) == failure_key(o2)


class _ReuseStepIdPlanner:
    """自定义 planner 故意复用已观测 step id（历史 bug 触发器）。"""

    async def plan(self, task, *, context):
        from agentix.domain.agent.types import Plan, PlanStep

        return Plan(
            steps=(PlanStep(id="s1", action="act", tool="act"),),
            version=1,
            planner="custom",
        )


@pytest.mark.asyncio
async def test_custom_planner_step_ids_are_remapped_around_observations():
    """自定义 planner 返回与失败步同 id 时必须重映射，否则 _next_pending 会跳过。"""
    tool = _FailThenPassTool(fail_times=1)
    rp = RetryReplanner(max_replans=3, same_failure_threshold=3, planner=_ReuseStepIdPlanner())
    loop = AgentLoop(
        planner=SingleStepPlanner(),
        context=SimpleContextBuilder(),
        verifier=_PassVerifier(),
        executor=MappingStepExecutor({"act": tool}),
        max_steps=10,
        max_replans=3,
        replanner=rp,
    )
    result = await loop.run(TaskSpec(goal="g", task_id="custom_remap"))
    assert result.replans >= 1
    assert tool.calls >= 2
    ids = [o.step_id for o in result.observations]
    assert len(ids) == len(set(ids))
    assert result.terminal in {"completed", "verify_failed", "budget_exceeded", "failed"}
