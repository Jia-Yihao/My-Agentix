"""R3：Agent Runtime 骨架 + 统一 ToolExecutor 测试。

依据：《GustoBot 生产级 Agent 系统架构与实现设计》（应用到 My Agentix，名称不变）
§7-11（Lifecycle/AgentState/ExecutionStep/Budget）、§16/§19/§20
（ToolSpec 扩展/ToolExecutor/ToolResult）、§100 Phase 1。

P0 验收：Runtime 包装既有执行不改变业务语义；预算超限 STOP；
工具执行统一走 Executor（权限/风险/超时/重试/轨迹）。
"""

from __future__ import annotations

import asyncio

import pytest

from agentix.runtime import (
    AgentRuntime,
    AgentState,
    AgentStatus,
    Budget,
    BudgetExceededError,
    BudgetManager,
    ExecutionStep,
    InvalidTransitionError,
    LifecycleManager,
    StepStatus,
    StepType,
    Task,
)
from agentix.tools.definitions import ToolSpec, registry as tool_registry
from agentix.tools.gateway.executor import ToolExecutor, ToolResult, reset_tool_executor


# ---------------------------------------------------------------- 生命周期状态机


def test_lifecycle_happy_path():
    state = AgentState(run_id="r1")
    LifecycleManager.transition(state, AgentStatus.RUNNING)
    LifecycleManager.transition(state, AgentStatus.PLANNING)
    LifecycleManager.transition(state, AgentStatus.EXECUTING)
    LifecycleManager.transition(state, AgentStatus.VERIFYING)
    LifecycleManager.transition(state, AgentStatus.COMPLETED)
    assert state.status is AgentStatus.COMPLETED
    assert LifecycleManager.is_terminal(state)


def test_lifecycle_illegal_jump_rejected():
    state = AgentState(run_id="r1")
    with pytest.raises(InvalidTransitionError):
        LifecycleManager.transition(state, AgentStatus.COMPLETED)  # CREATED → COMPLETED 非法


def test_lifecycle_terminal_has_no_exit():
    state = AgentState(run_id="r1")
    LifecycleManager.transition(state, AgentStatus.RUNNING)
    LifecycleManager.transition(state, AgentStatus.CANCELLED)
    with pytest.raises(InvalidTransitionError):
        LifecycleManager.transition(state, AgentStatus.RUNNING)


def test_lifecycle_fail_writes_error_and_is_terminal_safe():
    state = AgentState(run_id="r1")
    LifecycleManager.transition(state, AgentStatus.RUNNING)
    LifecycleManager.fail(state, "TOOL_FAILED", "neo4j down", stage="tool")
    assert state.status is AgentStatus.FAILED
    assert state.error.code == "TOOL_FAILED"
    assert state.error.stage == "tool"
    # 终态再 fail：不覆盖既有结论
    LifecycleManager.fail(state, "OTHER", "second failure")
    assert state.error.code == "TOOL_FAILED"


def test_lifecycle_approval_flow():
    # A4 死转移处置：审批流唯一状态是 WAITING_APPROVAL
    # （PAUSED_FOR_APPROVAL 已移除，原边 EXECUTING→PAUSED_FOR_APPROVAL 并入此处）
    state = AgentState(run_id="r1")
    LifecycleManager.transition(state, AgentStatus.RUNNING)
    LifecycleManager.transition(state, AgentStatus.EXECUTING)
    LifecycleManager.transition(state, AgentStatus.WAITING_APPROVAL)
    LifecycleManager.transition(state, AgentStatus.RUNNING)
    assert state.status is AgentStatus.RUNNING


# ---------------------------------------------------------------- ExecutionStep


def test_execution_step_complete_computes_latency():
    step = ExecutionStep(step_type=StepType.TOOL, name="legal_search")
    step.start()
    step.complete({"hits": 3})
    assert step.status is StepStatus.SUCCEEDED
    assert step.output == {"hits": 3}
    assert step.latency_ms is not None and step.latency_ms >= 0
    assert step.completed_at is not None


def test_execution_step_fail_and_skip():
    step = ExecutionStep(step_type=StepType.VERIFY, name="citation_validator")
    step.fail("mismatch")
    assert step.status is StepStatus.FAILED
    assert step.error == "mismatch"

    step2 = ExecutionStep(step_type=StepType.GENERATE, name="final_answer")
    step2.skip("budget exceeded")
    assert step2.status is StepStatus.SKIPPED


def test_execution_step_to_dict():
    step = ExecutionStep(step_type=StepType.PLAN, name="planner", tool_call_id="c1")
    step.complete()
    d = step.to_dict()
    assert d["step_type"] == "plan"
    assert d["name"] == "planner"
    assert d["status"] == "succeeded"
    assert d["tool_call_id"] == "c1"
    assert "step_id" in d and "latency_ms" in d


# ---------------------------------------------------------------- 预算


def test_budget_default_limits():
    budget = Budget()
    assert budget.max_steps == 20
    assert budget.max_llm_calls == 15
    assert budget.max_tool_calls == 20
    assert budget.max_tokens == 30000
    assert budget.max_cost_usd == 0.5


def test_budget_manager_step_exceeded():
    manager = BudgetManager(budget=Budget(max_steps=2))
    manager.record_step()
    manager.check_step()
    manager.record_step()
    with pytest.raises(BudgetExceededError) as exc_info:
        manager.check_step()
    assert exc_info.value.dimension == "steps"


def test_budget_manager_tool_call_exceeded():
    manager = BudgetManager(budget=Budget(max_tool_calls=1))
    manager.record_tool_call()
    with pytest.raises(BudgetExceededError) as exc_info:
        manager.check_tool_call()
    assert exc_info.value.dimension == "tool_calls"


def test_budget_manager_token_exceeded_on_record():
    manager = BudgetManager(budget=Budget(max_tokens=100))
    manager.record_llm_call(tokens=60)
    with pytest.raises(BudgetExceededError) as exc_info:
        manager.record_llm_call(tokens=60)
    assert exc_info.value.dimension == "tokens"
    assert manager.used_tokens == 120


def test_budget_manager_zero_means_unlimited():
    manager = BudgetManager(budget=Budget(max_steps=0, max_tokens=0))
    for _ in range(100):
        manager.record_step()
        manager.check_step()
        manager.record_llm_call(tokens=10**6)


def test_budget_snapshot():
    manager = BudgetManager(budget=Budget(max_steps=5))
    manager.record_step()
    manager.record_llm_call(tokens=50)
    snap = manager.snapshot()
    assert snap.max_steps == 5
    assert snap.used_steps == 1
    assert snap.used_tokens == 50
    assert snap.elapsed_ms >= 0


# ---------------------------------------------------------------- AgentState


def test_agent_state_defaults_and_to_dict():
    state = AgentState(run_id="r42", session_id="s1", user_id="u1", task=Task(kind="kb_qa", question="q"))
    state.messages.append({"role": "user", "content": "q"})
    d = state.to_dict()
    assert d["run_id"] == "r42"
    assert d["task"]["kind"] == "kb_qa"
    assert d["status"] == "created"
    assert d["steps"] == []
    assert d["error"] is None


# ---------------------------------------------------------------- AgentRuntime


def test_runtime_begin_and_finish():
    runtime = AgentRuntime(session_id="s1", user_id="u1", budget=Budget(max_steps=10))
    state = runtime.begin(task=Task(kind="legal_research", question="试用期规定"))
    assert state.status is AgentStatus.RUNNING
    assert state.run_id == runtime.run_id
    # 幂等 begin
    assert runtime.begin() is state

    state.steps.append(ExecutionStep(step_type=StepType.ROUTE, name="router"))
    state.steps[-1].complete()
    runtime.finish(output={"answer": "ok"})
    assert state.status is AgentStatus.COMPLETED
    assert state.budget.max_steps == 10
    assert state.artifacts[-1]["final"] == {"answer": "ok"}


def test_runtime_step_context_success():
    runtime = AgentRuntime(session_id="s1")
    runtime.begin()
    with runtime.step(StepType.PLAN, "planner", input_data={"q": "x"}) as st:
        st.complete({"plan": 2})
    step = runtime.state.steps[0]
    assert step.status is StepStatus.SUCCEEDED
    assert step.output == {"plan": 2}
    assert runtime.budget_manager.used_steps == 1


def test_runtime_step_context_failure_reraises_and_records():
    runtime = AgentRuntime(session_id="s1")
    runtime.begin()
    with pytest.raises(ValueError):
        with runtime.step(StepType.TOOL, "bad_tool"):
            raise ValueError("boom")
    step = runtime.state.steps[0]
    assert step.status is StepStatus.FAILED
    assert "boom" in (step.error or "")


def test_runtime_budget_stop_on_max_steps():
    runtime = AgentRuntime(session_id="s1", budget=Budget(max_steps=1))
    runtime.begin()
    with runtime.step(StepType.ROUTE, "router"):
        pass
    with pytest.raises(BudgetExceededError):
        with runtime.step(StepType.PLAN, "planner"):
            pass  # pragma: no cover - 不会执行到这里
    # 超限步骤在创建前即被拦截：轨迹只有第一步，预算不被虚假步数污染
    assert len(runtime.state.steps) == 1
    assert runtime.state.steps[0].name == "router"


@pytest.mark.asyncio
async def test_runtime_run_graph_passthrough():
    """既有 LangGraph 图原样执行（Strangler Phase 1），Runtime 只记账。"""

    class FakeGraph:
        def __init__(self):
            self.calls = []

        async def ainvoke(self, graph_input, config=None):
            self.calls.append((graph_input, config))
            return {"messages": ["answer"]}

    graph = FakeGraph()
    runtime = AgentRuntime(session_id="s1", budget=Budget(max_latency_ms=10_000))
    runtime.begin()
    result = await runtime.run_graph(graph, {"question": "q"}, config={"tags": ["t"]})
    assert result == {"messages": ["answer"]}
    assert graph.calls == [({"question": "q"}, {"tags": ["t"]})]
    step = runtime.state.steps[0]
    assert step.step_type == StepType.EXECUTING
    assert step.status is StepStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_runtime_run_graph_failure_aborts():
    class FailingGraph:
        async def ainvoke(self, graph_input, config=None):
            raise RuntimeError("graph exploded")

    runtime = AgentRuntime(session_id="s1")
    runtime.begin()
    with pytest.raises(RuntimeError):
        await runtime.run_graph(FailingGraph(), {})
    step = runtime.state.steps[0]
    assert step.status is StepStatus.FAILED


def test_runtime_abort():
    runtime = AgentRuntime(session_id="s1")
    runtime.begin()
    runtime.abort("LLM_TIMEOUT", "llm call timed out", stage="generate", retryable=True)
    state = runtime.state
    assert state.status is AgentStatus.FAILED
    assert state.error.code == "LLM_TIMEOUT"
    assert state.error.retryable is True


# ---------------------------------------------------------------- ToolSpec 扩展


def test_toolspec_execution_governance_defaults():
    spec = ToolSpec(name="t1", description="d")
    assert spec.capabilities == ()
    assert spec.side_effect is False
    assert spec.timeout_ms == 30_000
    assert spec.retryable is False
    assert spec.max_retries == 2


def test_toolspec_invalid_timeout_and_retries():
    with pytest.raises(ValueError):
        ToolSpec(name="t", description="d", timeout_ms=0)
    with pytest.raises(ValueError):
        ToolSpec(name="t", description="d", max_retries=-1)


def test_toolspec_side_effect_must_not_retry():
    """有副作用工具禁止自动重试（防重复写入）。"""
    with pytest.raises(ValueError):
        ToolSpec(name="t", description="d", side_effect=True, retryable=True)


def test_toolspec_backward_compatible_construction():
    """既有声明式用法（仅 name/description/parameters/risk_level）不受影响。"""
    spec = ToolSpec(
        name="cypher_query",
        description="d",
        parameters={"type": "object"},
        risk_level="L0",
    )
    assert spec.timeout_ms == 30_000


# ---------------------------------------------------------------- ToolExecutor


class _Policy:
    def __init__(self, max_risk_level: str = "L1"):
        self.max_risk_level = max_risk_level


@pytest.mark.asyncio
async def test_executor_unknown_tool():
    executor = ToolExecutor()
    result = await executor.execute("nope", {})
    assert result.success is False
    assert "unknown tool" in result.error
    assert result.retryable is False


@pytest.mark.asyncio
async def test_executor_success_sync_and_async():
    executor = ToolExecutor()
    executor.register_handler(
        "sync_tool", lambda args: {"echo": args["x"]},
        spec=ToolSpec(name="sync_tool", description="d"),
    )

    async def async_handler(args):
        await asyncio.sleep(0.01)
        return {"async": True}

    executor.register_handler(
        "async_tool", async_handler,
        spec=ToolSpec(name="async_tool", description="d", retryable=True),
    )

    r1 = await executor.execute("sync_tool", {"x": 1})
    assert r1.success is True and r1.data == {"echo": 1}
    assert r1.call_id and r1.latency_ms >= 0

    r2 = await executor.execute("async_tool", {})
    assert r2.success is True and r2.data == {"async": True}
    assert r2.retryable is True


@pytest.mark.asyncio
async def test_executor_timeout_enforced():
    executor = ToolExecutor()

    async def slow_handler(args):
        await asyncio.sleep(1.0)

    executor.register_handler(
        "slow", slow_handler,
        spec=ToolSpec(name="slow", description="d", timeout_ms=50),
    )
    result = await executor.execute("slow", {})
    assert result.success is False
    assert "timeout" in result.error
    assert result.retryable is False  # 未声明 retryable → 不重试


@pytest.mark.asyncio
async def test_executor_retry_then_succeed():
    executor = ToolExecutor()
    calls = {"n": 0}

    async def flaky(args):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("flaky")
        return {"ok": True}

    executor.register_handler(
        "flaky", flaky,
        spec=ToolSpec(name="flaky", description="d", retryable=True, max_retries=3),
    )
    result = await executor.execute("flaky", {})
    assert result.success is True
    assert result.metadata.get("retry_count") == 2
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_executor_retries_exhausted():
    executor = ToolExecutor()

    async def always_fails(args):
        raise RuntimeError("down")

    executor.register_handler(
        "down", always_fails,
        spec=ToolSpec(name="down", description="d", retryable=True, max_retries=1),
    )
    result = await executor.execute("down", {})
    assert result.success is False
    assert "RuntimeError" in result.error
    assert result.retryable is True


@pytest.mark.asyncio
async def test_executor_permission_denied_and_fail_closed():
    executor = ToolExecutor(permission_checker=lambda name, spec, policy: False)
    executor.register_handler(
        "t", lambda args: {}, spec=ToolSpec(name="t", description="d")
    )
    result = await executor.execute("t", {})
    assert result.success is False
    assert result.metadata["reason"] == "permission_denied"

    # 权限钩子自身抛异常 → fail-closed 拒绝
    def broken_checker(name, spec, policy):
        raise RuntimeError("hook broken")

    executor2 = ToolExecutor(permission_checker=broken_checker)
    executor2.register_handler(
        "t2", lambda args: {}, spec=ToolSpec(name="t2", description="d")
    )
    result2 = await executor2.execute("t2", {})
    assert result2.success is False
    assert result2.metadata["reason"] == "permission_denied"


@pytest.mark.asyncio
async def test_executor_risk_gate():
    executor = ToolExecutor()
    executor.register_handler(
        "write_tool", lambda args: {},
        spec=ToolSpec(
            name="write_tool", description="d", risk_level="L2",
            side_effect=True, control="需人工审批（HITL）",
        ),
    )
    # 策略只允许 L1 → 拒绝
    result = await executor.execute("write_tool", {}, policy=_Policy(max_risk_level="L1"))
    assert result.success is False
    assert result.metadata["reason"] == "risk_denied"

    # 策略允许 L2 → 放行
    result_ok = await executor.execute("write_tool", {}, policy=_Policy(max_risk_level="L2"))
    assert result_ok.success is True


def test_toolresult_shape():
    result = ToolResult(tool_name="t", success=True, data={"a": 1}, latency_ms=5)
    assert result.call_id
    assert result.citations == []
    assert result.metadata == {}
    assert result.error is None


@pytest.fixture(autouse=True)
def _reset_executor_singleton():
    # register_handler 会把 spec 自动登记进全局 tool_registry；
    # 快照名称集合，结束后删除新增项，防止 L2 夹具泄漏打红
    # test_r1_tool_risk_levels 的"全部 L0"断言（测试顺序污染）。
    before = set(tool_registry.tool_names())
    reset_tool_executor()
    yield
    reset_tool_executor()
    for name in set(tool_registry.tool_names()) - before:
        tool_registry.delete_tool(name)
