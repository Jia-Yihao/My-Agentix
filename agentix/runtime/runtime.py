"""AgentRuntime——包装既有 LangGraph 执行的统一 Runtime（文档 §100 Phase 1）。

P0 定位：**Runtime ↓ Existing LangGraph**——在不改变业务能力的前提下
获得统一的执行上下文（AgentState）、步骤轨迹（ExecutionStep）、预算
（BudgetManager）与生命周期（LifecycleManager）。

用法（包装既有图执行）::

    runtime = AgentRuntime(session_id=sid, user_id=uid)
    state = runtime.begin(task=Task(kind="legal_research", question=q))

    async with runtime.step(StepType.PLAN, "legal_research_planner") as st:
        plan = await planner_node(...)
        st.complete({"steps": len(plan)})

    result = await runtime.run_graph(graph, graph_input)   # 既有图
    runtime.finish()

超出预算（步数/工具/LLM/耗时）会抛 :class:`BudgetExceededError`，
调用方决定 STOP 或降级；Runtime 自身绝不静默吞掉。
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Awaitable, Callable

from agentix.domain.runtime.budget import Budget
from agentix.runtime.budget import BudgetManager
from agentix.runtime.lifecycle import LifecycleManager
from agentix.domain.runtime.steps import StepStatus
from agentix.runtime.state import (
    AgentState,
    AgentStatus,
    PolicyContext,
    Task,
)
from agentix.domain.runtime.steps import StepType
from agentix.runtime.steps import ExecutionStep

logger = logging.getLogger(__name__)


class AgentRuntime:
    """一次 Agent 运行的 Runtime 门面：状态 + 步骤 + 预算 + 生命周期。"""

    def __init__(
        self,
        *,
        run_id: str | None = None,
        session_id: str = "",
        user_id: str = "",
        budget: Budget | None = None,
        policy: PolicyContext | None = None,
    ):
        self.run_id = run_id or uuid.uuid4().hex[:16]
        self._budget_manager = BudgetManager(budget=budget or Budget())
        self._state: AgentState | None = None
        self._policy = policy or PolicyContext()

    # -- 生命周期 --------------------------------------------------------

    @property
    def state(self) -> AgentState:
        if self._state is None:
            raise RuntimeError("runtime not started: call begin() first")
        return self._state

    def begin(
        self,
        *,
        task: Task | None = None,
        session_id: str = "",
        user_id: str = "",
    ) -> AgentState:
        """创建 AgentState 并进入 RUNNING（幂等保护：重复 begin 返回既有 state）。"""
        if self._state is not None:
            return self._state
        self._state = AgentState(
            run_id=self.run_id,
            session_id=session_id,
            user_id=user_id,
            task=task or Task(),
            policy=self._policy,
            budget=self._budget_manager.snapshot(),
        )
        LifecycleManager.transition(self._state, AgentStatus.RUNNING)
        return self._state

    def finish(self, *, output: dict[str, Any] | None = None) -> AgentState:
        """正常收尾：VERIFYING → COMPLETED（预算快照写回 state）。"""
        state = self.state
        if LifecycleManager.is_terminal(state):
            return state
        if state.status != AgentStatus.VERIFYING:
            LifecycleManager.transition(state, AgentStatus.VERIFYING)
        state.budget = self._budget_manager.snapshot()
        LifecycleManager.transition(state, AgentStatus.COMPLETED)
        if output:
            state.artifacts.append({"final": output})
        return state

    def abort(self, code: str, message: str, *, stage: str | None = None,
              retryable: bool = False) -> AgentState:
        """异常收尾：转 FAILED 并写结构化错误。"""
        state = self.state
        state.budget = self._budget_manager.snapshot()
        return LifecycleManager.fail(state, code, message, stage=stage, retryable=retryable)

    # -- 步骤 ------------------------------------------------------------

    @contextmanager
    def step(self, step_type: str, name: str, *, tool_call_id: str | None = None,
             input_data: dict[str, Any] | None = None):
        """同步步骤上下文：自动计时、预算检查、写回 AgentState.steps。

        超步数预算时步骤标记 SKIPPED 并抛 :class:`BudgetExceededError`。
        """
        self._budget_manager.check_step()
        exec_step = ExecutionStep(
            step_type=step_type,
            name=name,
            tool_call_id=tool_call_id,
            input=input_data or {},
        )
        exec_step.start()
        state = self.state
        state.steps.append(exec_step)
        self._budget_manager.record_step()
        try:
            yield exec_step
        except Exception as exc:  # noqa: BLE001 - 步骤失败必须落账后重抛
            exec_step.fail(str(exc))
            self._mirror_trajectory(exec_step)
            raise
        else:
            # 用户在 with 体内已手动 complete/fail 时不再覆盖
            if exec_step.status is StepStatus.RUNNING:
                exec_step.complete()
            self._mirror_trajectory(exec_step)

    @asynccontextmanager
    async def astep(self, step_type: str, name: str, *, tool_call_id: str | None = None,
                    input_data: dict[str, Any] | None = None):
        """异步版本（:meth:`step` 的 async 形态，供 async 节点使用）。"""
        self._budget_manager.check_step()
        exec_step = ExecutionStep(
            step_type=step_type,
            name=name,
            tool_call_id=tool_call_id,
            input=input_data or {},
        )
        exec_step.start()
        self.state.steps.append(exec_step)
        self._budget_manager.record_step()
        try:
            yield exec_step
        except Exception as exc:  # noqa: BLE001
            exec_step.fail(str(exc))
            self._mirror_trajectory(exec_step)
            raise
        else:
            if exec_step.status is StepStatus.RUNNING:
                exec_step.complete()
            self._mirror_trajectory(exec_step)

    # -- 图执行包装 -------------------------------------------------------

    async def run_graph(
        self,
        graph: Any,
        graph_input: dict[str, Any],
        *,
        step_name: str = "langgraph_invoke",
        config: dict[str, Any] | None = None,
    ) -> Any:
        """把既有 LangGraph 图的一次 ``ainvoke`` 包成 EXECUTING 步骤。

        图保持原样（Strangler Phase 1），Runtime 只负责记账与轨迹。
        返回值与图 ``ainvoke`` 完全一致。
        """
        self._budget_manager.check_latency()
        async with self.astep(StepType.EXECUTING, step_name, input_data=graph_input):
            result = await graph.ainvoke(graph_input, config=config or {})
        self.state.budget = self._budget_manager.snapshot()
        return result

    async def run_callable(
        self,
        fn: Callable[..., Awaitable[Any]],
        *args: Any,
        step_name: str = "callable",
        **kwargs: Any,
    ) -> Any:
        """通用 async 可调用包装（非图场景的等价入口）。"""
        self._budget_manager.check_latency()
        async with self.astep(StepType.EXECUTING, step_name):
            return await fn(*args, **kwargs)

    # -- 预算透传 ---------------------------------------------------------

    @property
    def budget_manager(self) -> BudgetManager:
        return self._budget_manager

    def record_llm_call(self, tokens: int = 0, cost_usd: float = 0.0) -> None:
        self._budget_manager.record_llm_call(tokens=tokens, cost_usd=cost_usd)

    def record_tool_call(self, n: int = 1) -> None:
        self._budget_manager.record_tool_call(n)

    def check_tool_call(self) -> None:
        self._budget_manager.check_tool_call()

    # -- 内部 -------------------------------------------------------------

    def _mirror_trajectory(self, exec_step: ExecutionStep) -> None:
        """best-effort 镜像到观测层 trajectory（失败不影响主流程）。"""
        try:
            from agentix.observability.trajectory import current_recorder

            recorder = current_recorder()
            if recorder is not None:
                # begin_step 不入列（返回 dict）；接住返回值交给 end_step。
                # （旧写法 pop 上一条已完成步 → 新步丢失 + 历史被覆写）
                step = recorder.begin_step(
                    f"runtime.{exec_step.step_type}.{exec_step.name}"
                )
                recorder.end_step(
                    step,
                    status="ok" if exec_step.status.value == "succeeded" else exec_step.status.value,
                    attributes={
                        "step_id": exec_step.step_id,
                        "latency_ms": exec_step.latency_ms,
                    },
                )
        except Exception:  # noqa: BLE001 - 观测失败不影响执行
            logger.debug("trajectory mirror failed", exc_info=True)
