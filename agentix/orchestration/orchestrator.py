"""Agent Core 主循环（Phase 2–3）：Task → Context → Plan → Execute → Observe → Verify → Final。

纯编排：节点只调注入的 Protocol，不写业务、不 import LangGraph / infrastructure。

预算与纠偏（Phase 3）：
- ``max_steps`` 每步执行前检查 → 终态 ``budget_exceeded``（不 hang）；
- ``max_replans`` + ``replanner``：失败后产出**新** Plan（Append-only）；
- 连续同因失败由 RetryReplanner 熔断（same_failure_threshold）；
- 四层故障（书 §5.7）：API / TOOL / CONTEXT / CONTROL → Observation.failure_layer。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Protocol, Sequence

from agentix.agent.hooks import ON_ERROR, ON_RUN_END, ON_STEP_END, emit
from agentix.domain.agent.types import (
    ContextSnapshot,
    FailureLayer,
    Observation,
    Plan,
    PlanStep,
    TaskSpec,
    VerificationResult,
)
from agentix.domain.runtime.lifecycle import ExitReason, ExitStatus

__all__ = ["AgentLoop", "LoopResult", "StepExecutorPort"]


class StepExecutorPort(Protocol):
    """执行单个 PlanStep 并返回 Observation（工具编排在 executor 实现）。"""

    async def execute(
        self,
        task: TaskSpec,
        plan: Plan,
        step: PlanStep,
        context: ContextSnapshot,
    ) -> Observation:
        ...


@dataclass(frozen=True)
class LoopResult:
    """一次主循环的终态快照（always terminal：completed / failed / …）。"""

    task_id: str
    final: str
    passed: bool
    plan: Plan
    context: ContextSnapshot
    observations: tuple[Observation, ...] = ()
    verification: Optional[VerificationResult] = None
    reason: str = ""
    terminal: str = "completed"  # completed | failed | budget_exceeded | verify_failed
    steps_used: int = 0
    replans: int = 0

    # -- 统一退出契约（A1b：domain.status.ExitStatus/ExitReason） ------------
    # 只读派生属性（terminal 仍是唯一事实源，不新增可变状态）；str 枚举
    # 与裸字符串可直接比较（``exit_status == "failed"`` 为 True）。

    @property
    def exit_status(self) -> ExitStatus:
        """退出状态：正常走完 = completed；预算耗尽/验证不通过/异常 = failed。"""
        if self.terminal == "completed":
            return ExitStatus.COMPLETED
        return ExitStatus.FAILED

    @property
    def exit_reason(self) -> ExitReason:
        """退出原因：budget_exceeded ⇒ max_iterations；其余失败 ⇒ error。"""
        if self.terminal == "completed":
            return ExitReason.COMPLETED
        if self.terminal == "budget_exceeded":
            return ExitReason.MAX_ITERATIONS
        return ExitReason.ERROR

    @property
    def truncated(self) -> bool:
        """结果是否因预算截断（仅 budget_exceeded 语义；其余 False）。"""
        return self.terminal == "budget_exceeded"


@dataclass
class _RunState:
    observations: List[Observation] = field(default_factory=list)
    steps_used: int = 0
    replans: int = 0


class AgentLoop:
    """Harness 主循环骨架。

    :param max_steps: 步数预算（每步执行前 check；0 = 不限——不推荐生产）。
    :param max_replans: Phase 3 生效；Phase 2 固定 0。
    :param budget_observer: Phase 28（Ch10 §10.6）预算观察回调
        ``(max_steps, steps_used)``——仅在剩余 ≤2 步时于 :145 检查处后回调，
        由应用侧注入轨迹 append 通道；``None``（默认/flag-off）零调用，
        不改变任何既有行为与 ``budget_exceeded`` 语义。
    :param context_budget_tokens: P3（渐进上下文）每步重建上下文的 token
        预算。``0``（默认）= 只在启动时构建一次（trajectory=()、budget=0，
        与基线逐位等价）；``>0`` = 每步执行前按累计观测重建上下文并按该
        预算确定性裁剪（重建异常 ⇒ ``context_build_failed`` 终态，不 hang）。
        值由组合根注入（如 ``settings.AGENT_CONTEXT_BUDGET_TOKENS``），
        本模块保持不 import config 的纯编排约束。
    :param round_concurrency: F1d·A-P1-3 并发工具轮。``1``（默认）= 单步
        串行（与基线逐位等价）；``>1`` = 每轮把「依赖已满足的 ready 步」
        组批并发（派发前逐调用授权门、危险工具强制串行、批宽按剩余步数
        裁剪、观测按声明序落位、每批至多一次 replan）。值由组合根注入
        （如 ``settings.AGENT_TOOL_ROUND_CONCURRENCY``），同款纯编排约束。
    """

    def __init__(
        self,
        *,
        planner,
        context,
        verifier,
        executor: StepExecutorPort,
        max_steps: int = 20,
        max_replans: int = 0,
        replanner=None,
        budget_observer=None,
        context_budget_tokens: int = 0,
        round_concurrency: int = 1,
    ) -> None:
        self._planner = planner
        self._context = context
        self._verifier = verifier
        self._executor = executor
        self._max_steps = max(0, max_steps)
        self._max_replans = max(0, max_replans)
        self._replanner = replanner
        self._budget_observer = budget_observer
        self._context_budget = max(0, int(context_budget_tokens))
        self._round_concurrency = max(1, int(round_concurrency))

    async def run(self, task: TaskSpec) -> LoopResult:
        """跑满一轮到**某终态**；永不返回中间态。

        ``on_run_end`` 观测钩子（Phase 21）在 ``finally`` 触发——正常终态与
        意外逸出都恰好一次；``emit`` 在 ``HOOKS_ENABLED=false`` 时零开销。
        """
        result: LoopResult | None = None
        try:
            result = await self._run_inner(task)
            return result
        finally:
            emit(ON_RUN_END, {"task_id": task.task_id, "result": result})

    async def _run_inner(self, task: TaskSpec) -> LoopResult:
        """``run`` 主体（拆出以便 finally 包装配对 run_end 观测钩子）。"""
        state = _RunState()

        # 1) Context（确定性失败 → failed）
        # P3（渐进上下文）：budget>0 仅把预算传入构建（trajectory 为空时
        # 两种预算的输出逐字节一致，差异只在后续逐步重建）。
        try:
            context = await self._context.build(
                task, trajectory=(), budget_tokens=self._context_budget
            )
        except Exception as exc:  # noqa: BLE001 — 循环边界统一兜底
            emit(ON_ERROR, {"task_id": task.task_id, "phase": "context",
                            "error": f"{type(exc).__name__}: {exc}"})
            return LoopResult(
                task_id=task.task_id,
                final="",
                passed=False,
                plan=Plan(),
                context=ContextSnapshot(prompt=""),
                reason=f"context_build_failed: {type(exc).__name__}: {exc}",
                terminal="failed",
                steps_used=0,
            )

        # 2) Plan
        try:
            plan = await self._planner.plan(task, context=context)
        except Exception as exc:  # noqa: BLE001
            emit(ON_ERROR, {"task_id": task.task_id, "phase": "plan",
                            "error": f"{type(exc).__name__}: {exc}"})
            return LoopResult(
                task_id=task.task_id,
                final="",
                passed=False,
                plan=Plan(),
                context=context,
                reason=f"plan_failed: {type(exc).__name__}: {exc}",
                terminal="failed",
            )

        # 3) Execute steps (Append-only observations)
        while True:
            if self._max_steps and state.steps_used >= self._max_steps:
                return LoopResult(
                    task_id=task.task_id,
                    final=self._synthesize(state.observations),
                    passed=False,
                    plan=plan,
                    context=context,
                    observations=tuple(state.observations),
                    reason=f"budget_exceeded steps={state.steps_used}",
                    terminal="budget_exceeded",
                    steps_used=state.steps_used,
                    replans=state.replans,
                )
            # Phase 28（Ch10 §10.6 预算意识）：剩余 ≤2 步 ⇒ 回调注入的
            # budget_observer（轨迹 append 通道，不动静态前缀）。未注入
            # （默认/flag-off）零调用；observer 自吞异常，耗尽语义不变。
            if (
                self._budget_observer is not None
                and self._max_steps
                and self._max_steps - state.steps_used <= 2
            ):
                try:
                    self._budget_observer(self._max_steps, state.steps_used)
                except Exception:  # noqa: BLE001 - 观测失败不影响主链路
                    pass
            # F1d·A-P1-3（并发工具轮，flag-on 分支）：round_concurrency>1 时
            # 走批路径（授权门 → 危险串行 → 预算裁剪 → 批前重建 → gather）；
            # =1（默认）永不进入，下方单步路径与基线逐位等价。
            if self._round_concurrency > 1:
                done, plan, context, fatal = await self._run_parallel_round(
                    task, plan, state, context
                )
                if fatal is not None:
                    return fatal
                if done:
                    break
                continue
            step = self._next_pending(plan, state)
            if step is None:
                break
            # P3（渐进上下文，补「主循环一次性构建」缺口）：预算>0 时按累计
            # 观测逐步重建上下文（轨迹进 prompt + 预算确定性裁剪）；=0（默认）
            # 零重建，context 引用与基线逐位等价。重建异常 = 与初始构建同款
            # context_build_failed 终态（emit 后返回，不 hang、不降级继续）。
            if self._context_budget and state.observations:
                try:
                    context = await self._context.build(
                        task,
                        trajectory=tuple(state.observations),
                        budget_tokens=self._context_budget,
                    )
                except Exception as exc:  # noqa: BLE001 — 循环边界统一兜底
                    emit(ON_ERROR, {"task_id": task.task_id, "phase": "context",
                                    "error": f"{type(exc).__name__}: {exc}"})
                    return LoopResult(
                        task_id=task.task_id,
                        final="",
                        passed=False,
                        plan=plan,
                        context=context,
                        reason=f"context_build_failed: {type(exc).__name__}: {exc}",
                        terminal="failed",
                        steps_used=state.steps_used,
                        replans=state.replans,
                        observations=tuple(state.observations),
                    )
            try:
                obs = await self._executor.execute(task, plan, step, context)
            except Exception as exc:  # noqa: BLE001 — 工具/控制流异常 → Observation
                # 修复：classify_exception 定义在 agent/executor/executor（capabilities 侧无此符号，
                # 旧 import 会在 except 内再抛 ImportError 逸出 run()）
                from agentix.orchestration.executor import classify_exception

                obs = Observation(
                    step_id=step.id,
                    content=f"{type(exc).__name__}: {exc}",
                    ok=False,
                    failure_layer=classify_exception(exc),
                    error_code="tool_exec_failed",
                )
                emit(ON_ERROR, {"task_id": task.task_id, "phase": "step",
                                "step_id": step.id,
                                "error": f"{type(exc).__name__}: {exc}"})
            state.observations.append(obs)
            state.steps_used += 1
            emit(ON_STEP_END, {
                "task_id": task.task_id,
                "step_id": step.id,
                "index": state.steps_used,
                "ok": obs.ok,
                "failure_layer": obs.failure_layer,
                "error_code": obs.error_code,
            })

            if not obs.ok and self._replanner is not None and state.replans < self._max_replans:
                new_plan = await self._maybe_replan(task, plan, context, state, obs)
                if new_plan is not None:
                    plan = new_plan
                    state.replans += 1

        # 4) Verify
        try:
            verification = await self._verifier.verify(task, plan, state.observations)
        except Exception as exc:  # noqa: BLE001
            emit(ON_ERROR, {"task_id": task.task_id, "phase": "verify",
                            "error": f"{type(exc).__name__}: {exc}"})
            return LoopResult(
                task_id=task.task_id,
                final=self._synthesize(state.observations),
                passed=False,
                plan=plan,
                context=context,
                observations=tuple(state.observations),
                reason=f"verify_failed: {type(exc).__name__}: {exc}",
                terminal="failed",
                steps_used=state.steps_used,
                replans=state.replans,
            )

        final = self._synthesize(state.observations)
        if verification.passed:
            terminal = "completed"
            reason = verification.reason
        else:
            terminal = "verify_failed"
            reason = verification.reason or "verifier_rejected"

        return LoopResult(
            task_id=task.task_id,
            final=final,
            passed=verification.passed,
            plan=plan,
            context=context,
            observations=tuple(state.observations),
            verification=verification,
            reason=reason,
            terminal=terminal,
            steps_used=state.steps_used,
            replans=state.replans,
        )

    def _next_pending(self, plan: Plan, state: _RunState) -> Optional[PlanStep]:
        """按声明顺序取第一个「依赖已满足且未执行」的步（简化：顺序执行）。"""
        done_ids = {o.step_id for o in state.observations}
        for step in plan.steps:
            if step.id in done_ids:
                continue
            if all(dep in done_ids or dep not in {s.id for s in plan.steps} for dep in step.depends_on):
                return step
        return None

    # -- F1d·A-P1-3（并发工具轮，flag-on 批路径） --------------------------

    @staticmethod
    def _step_tool_name(step: PlanStep) -> str:
        """与 ``executor.execute`` 同款取名（``step.tool or step.action``）。"""
        return str(step.tool or step.action or "")

    def _ready_steps(self, plan: Plan, state: _RunState) -> List[PlanStep]:
        """``_next_pending`` 同款谓词的**全量** ready 步（声明序）。"""
        done_ids = {o.step_id for o in state.observations}
        plan_ids = {s.id for s in plan.steps}
        return [
            s
            for s in plan.steps
            if s.id not in done_ids
            and all(dep in done_ids or dep not in plan_ids for dep in s.depends_on)
        ]

    def _preflight_block(self, step: PlanStep) -> Optional[str]:
        """授权门（执行点语义，惰性 import；闸件不可用 fail-closed）。"""
        try:
            from agentix.tools.gateway.round_gate import preflight_block
        except Exception:  # noqa: BLE001 - 闸件不可用 ⇒ fail-closed（同 ToolExecutor）
            return "review gate unavailable (fail-closed)"
        return preflight_block(self._step_tool_name(step))

    def _step_dangerous(self, step: PlanStep) -> bool:
        """危险判定（惰性 import；判定不可用 ⇒ 保守按危险串行）。"""
        try:
            from agentix.tools.gateway.round_gate import is_dangerous
        except Exception:  # noqa: BLE001 - 判定不可用 ⇒ 保守串行
            return True
        return is_dangerous(self._step_tool_name(step))

    async def _run_parallel_round(
        self,
        task: TaskSpec,
        plan: Plan,
        state: _RunState,
        context: ContextSnapshot,
    ):
        """flag-on 并发轮：ready 批发执行。

        顺序 = 授权门（逐调用，被拦 ⇒ failed Observation 且不派发）→
        危险强制串行（executable 含危险步 ⇒ 只取第一个危险步成批）→
        预算裁剪（并发不放宽 ``max_steps``）→ 批前一次上下文重建 →
        ``Semaphore(round_concurrency)`` gather → 按声明序落位 + 单调
        ``steps_used``/``ON_STEP_END`` → 每批至多一次 replan。

        :returns: ``(done, plan, context, fatal)``——``done``=无 ready 步回
            verify；``fatal``=重建失败的终态 LoopResult（非 None 时直接返回）。
        """
        import asyncio

        ready = self._ready_steps(plan, state)
        if not ready:
            return True, plan, context, None

        # 1) 预算裁剪（G7：并发不放宽步数预算；调用点预算闸保证 remaining≥1）
        if self._max_steps:
            remaining = self._max_steps - state.steps_used
            eligible = ready[: max(0, remaining)]
        else:
            eligible = ready
        if not eligible:
            # 不可达（调用点预算闸保证 remaining≥1）：交回循环顶由预算闸定终态
            return False, plan, context, None

        # 2) 授权门（批前逐调用；被拦步消耗一次尝试，绝不进入派发集）
        blocked: dict[str, Observation] = {}
        executable: List[PlanStep] = []
        for s in eligible:
            msg = self._preflight_block(s)
            if msg:
                blocked[s.id] = Observation(
                    step_id=s.id,
                    content=msg,
                    ok=False,
                    failure_layer=FailureLayer.TOOL,
                    error_code="review_required",
                )
            else:
                executable.append(s)

        # 3) 危险强制串行：executable 含危险步 ⇒ 只取第一个危险步单成一批，
        #    其余步保持 ready 下轮再入（危险绝不与任何调用并发）
        dangerous = next((s for s in executable if self._step_dangerous(s)), None)
        batch = [dangerous] if dangerous is not None else executable

        # 4) 批前一次上下文重建（批成员共享同一前批上下文；=0 零重建与基线同款）
        if self._context_budget and state.observations:
            try:
                context = await self._context.build(
                    task,
                    trajectory=tuple(state.observations),
                    budget_tokens=self._context_budget,
                )
            except Exception as exc:  # noqa: BLE001 — 循环边界统一兜底
                emit(ON_ERROR, {"task_id": task.task_id, "phase": "context",
                                "error": f"{type(exc).__name__}: {exc}"})
                return True, plan, context, LoopResult(
                    task_id=task.task_id,
                    final="",
                    passed=False,
                    plan=plan,
                    context=context,
                    reason=f"context_build_failed: {type(exc).__name__}: {exc}",
                    terminal="failed",
                    steps_used=state.steps_used,
                    replans=state.replans,
                    observations=tuple(state.observations),
                )

        # 5) 批执行：Semaphore 限并发，异常按单步路径同款分类成 Observation
        results: dict[str, Observation] = {}
        if batch:
            sem = asyncio.Semaphore(self._round_concurrency)

            async def _one(st: PlanStep) -> Observation:
                async with sem:
                    try:
                        return await self._executor.execute(task, plan, st, context)
                    except Exception as exc:  # noqa: BLE001 — 同单步路径分类
                        from agentix.orchestration.executor import classify_exception

                        emit(ON_ERROR, {"task_id": task.task_id, "phase": "step",
                                        "step_id": st.id,
                                        "error": f"{type(exc).__name__}: {exc}"})
                        return Observation(
                            step_id=st.id,
                            content=f"{type(exc).__name__}: {exc}",
                            ok=False,
                            failure_layer=classify_exception(exc),
                            error_code="tool_exec_failed",
                        )

            outs = await asyncio.gather(*(_one(st) for st in batch))
            for st, out in zip(batch, outs):
                results[st.id] = out

        # 6) 落位：eligible 内按声明序合并 blocked/已执行（危险串行推迟的步
        #    不落位、不计步，下一轮仍是 ready）；steps_used 单调
        appended: List[Observation] = []
        for st in eligible:
            if st.id in blocked:
                obs = blocked[st.id]
            elif st.id in results:
                obs = results[st.id]
            else:
                continue
            state.observations.append(obs)
            state.steps_used += 1
            emit(ON_STEP_END, {
                "task_id": task.task_id,
                "step_id": st.id,
                "index": state.steps_used,
                "ok": obs.ok,
                "failure_layer": obs.failure_layer,
                "error_code": obs.error_code,
            })
            appended.append(obs)

        # 7) 每批至多一次 replan（取声明序首个失败观测，语义同单步路径）
        first_fail = next((o for o in appended if not o.ok), None)
        if (
            first_fail is not None
            and self._replanner is not None
            and state.replans < self._max_replans
        ):
            new_plan = await self._maybe_replan(task, plan, context, state, first_fail)
            if new_plan is not None:
                plan = new_plan
                state.replans += 1

        return False, plan, context, None

    async def _maybe_replan(
        self,
        task: TaskSpec,
        plan: Plan,
        context: ContextSnapshot,
        state: _RunState,
        obs: Observation,
    ) -> Optional[Plan]:
        if self._replanner is None:
            return None
        try:
            new_plan = await self._replanner.replan(
                task, plan, context, state.observations, last_failure=obs
            )
            return None if new_plan is None else new_plan
        except Exception:  # noqa: BLE001 — 重规划失败不打断终态
            return None

    @staticmethod
    def _synthesize(observations: Sequence[Observation]) -> str:
        """从成功观测合成最终文本（Harness 级最小合成；业务合成在上层）。"""
        parts = [o.content for o in observations if o.ok and o.content]
        if parts:
            return parts[-1]
        if observations:
            return observations[-1].content
        return ""
