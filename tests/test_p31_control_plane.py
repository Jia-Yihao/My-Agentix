"""P31：控制平面（spawn/status/cancel/预算）+ token 预算 + 目标仲裁。

覆盖《My Agentix_架构优化方案》§9.1 D 组缺口：
- 「控制平面具备 spawn/status/cancel/预算分配」
- 「有目标优先级与冲突仲裁规则」
- 「硬约束最大轮数/并发/token 预算」（token 预算部分）

全部离线，零外部依赖。
"""

from __future__ import annotations

import asyncio

import pytest

from agentix.runtime.control_plane import (
    ControlPlane,
    RunState,
    reset_control_plane,
)
from agentix.application.services.token_budget import (
    BudgetLedger,
    BudgetVerdict,
    TokenBudget,
)
from agentix.domain.policies.goal_arbitration import (
    CONFLICTING_PAIRS,
    PRECEDENCE,
    Goal,
    GoalKind,
    arbitrate,
    dominant_goal,
)


# ---------------------------------------------------------------------------
# Token 预算
# ---------------------------------------------------------------------------


class TestTokenBudget:
    def test_disabled_budget_never_exceeds(self):
        budget = TokenBudget(limit=0, used=10**9)
        assert budget.enabled is False
        assert budget.verdict() == BudgetVerdict.OK
        assert budget.remaining is None       # 不限额 ≠ 0 剩余
        assert budget.exhausted is False
        assert budget.would_exceed(10**9) is False

    def test_remaining_and_ratio(self):
        budget = TokenBudget(limit=100, used=25)
        assert budget.remaining == 75
        assert budget.usage_ratio == 0.25
        assert budget.verdict() == BudgetVerdict.OK

    def test_warn_threshold(self):
        budget = TokenBudget(limit=100, used=80, warn_ratio=0.8)
        assert budget.verdict() == BudgetVerdict.WARN

    def test_exceeded_takes_precedence_over_warn(self):
        budget = TokenBudget(limit=100, used=100, warn_ratio=0.8)
        assert budget.verdict() == BudgetVerdict.EXCEEDED
        assert budget.exhausted is True
        assert budget.remaining == 0

    def test_consume_and_refund(self):
        budget = TokenBudget(limit=100)
        budget.consume(30)
        assert budget.used == 30
        budget.consume(-10)  # 退还
        assert budget.used == 20
        budget.consume(-1000)  # 不会变成负数
        assert budget.used == 0

    def test_would_exceed_precheck(self):
        budget = TokenBudget(limit=100, used=90)
        assert budget.would_exceed(10) is False
        assert budget.would_exceed(11) is True

    def test_to_dict(self):
        data = TokenBudget(limit=100, used=50).to_dict()
        assert data["verdict"] == "ok"
        assert data["remaining"] == 50
        assert data["usage_ratio"] == 0.5


class TestBudgetLedger:
    def test_disabled_request_yields_disabled_agents(self):
        """请求不限额 ⇒ 子 Agent 也不限额（默认关闭语义保持）。"""
        ledger = BudgetLedger.from_settings(per_request=0)
        agent = ledger.for_agent("router")
        assert agent.enabled is False
        assert ledger.verdict(agent="router") == BudgetVerdict.OK

    def test_fair_share_when_no_agent_limit(self):
        ledger = BudgetLedger.from_settings(per_request=100)
        first = ledger.for_agent("a")
        second = ledger.for_agent("b")
        assert first.limit == 100      # 第一个：100 // 1
        assert second.limit == 50      # 第二个：100 // 2
        assert second.limit <= ledger.request.limit

    def test_explicit_agent_limit_respected(self):
        ledger = BudgetLedger.from_settings(per_request=1000, per_agent=200)
        assert ledger.for_agent("x").limit == 200

    def test_agent_limit_capped_by_request(self):
        ledger = BudgetLedger.from_settings(per_request=100, per_agent=9999)
        assert ledger.for_agent("x").limit == 100

    def test_consume_records_both_levels(self):
        ledger = BudgetLedger.from_settings(per_request=1000, per_agent=500)
        ledger.consume(120, agent="router")
        assert ledger.request.used == 120
        assert ledger.for_agent("router").used == 120
        assert ledger.agent_totals() == 120

    def test_verdict_takes_worst(self):
        """子 Agent 超限 ⇒ 综合判定为 EXCEEDED，即使请求级还没超。"""
        ledger = BudgetLedger.from_settings(per_request=1000, per_agent=100)
        ledger.consume(150, agent="router")
        assert ledger.request.verdict() == BudgetVerdict.OK
        assert ledger.verdict(agent="router") == BudgetVerdict.EXCEEDED

    def test_unattributed_usage(self):
        ledger = BudgetLedger.from_settings(per_request=1000, per_agent=500)
        ledger.consume(100)  # 未归属
        ledger.consume(50, agent="a")
        data = ledger.to_dict()
        assert ledger.request.used == 150
        assert data["agent_total_used"] == 50
        # 未归属 = 请求级总量 - 已归属子 Agent 总量
        assert data["unattributed_used"] == 100

    def test_snapshot_lines(self):
        ledger = BudgetLedger.from_settings(per_request=100, per_agent=50)
        ledger.consume(10, agent="a")
        lines = ledger.snapshot_lines()
        assert any("request" in line for line in lines)
        assert any("agent=a" in line for line in lines)


# ---------------------------------------------------------------------------
# 控制平面
# ---------------------------------------------------------------------------


class TestBudgetEnforcement:
    """token 预算从「只计量」升级为「真限额」（批次 13）。

    关键：**默认关闭时必须完全 inert** —— `TOKEN_BUDGET_PER_REQUEST=0` ⇒
    不绑定台账 ⇒ `budget_exhausted_error()` 恒返回 None ⇒ 零行为变化。
    """

    def setup_method(self):
        from agentix.application.services.token_budget import clear_current_ledger

        clear_current_ledger()

    def teardown_method(self):
        from agentix.application.services.token_budget import clear_current_ledger

        clear_current_ledger()

    def test_no_ledger_is_inert(self):
        from agentix.application.services.token_budget import budget_exhausted_error

        assert budget_exhausted_error("router") is None

    def test_no_current_ledger_returns_none(self):
        from agentix.application.services.token_budget import get_current_ledger

        assert get_current_ledger() is None

    def test_within_budget_is_allowed(self):
        from agentix.application.services.token_budget import (
            BudgetLedger,
            budget_exhausted_error,
            set_current_ledger,
        )

        ledger = BudgetLedger.from_settings(per_request=1000, per_agent=100)
        ledger.consume(10, agent="router")
        set_current_ledger(ledger)
        assert budget_exhausted_error("router") is None

    def test_exhausted_budget_raises_degradable_agent_error(self):
        from agentix.application.services.token_budget import (
            BudgetLedger,
            budget_exhausted_error,
            set_current_ledger,
        )
        from agentix.domain.errors import DEGRADABLE, AgentError, ErrorCode

        ledger = BudgetLedger.from_settings(per_request=1000, per_agent=100)
        ledger.consume(1009, agent="router")
        set_current_ledger(ledger)

        err = budget_exhausted_error("router")
        assert isinstance(err, AgentError)
        assert err.code is ErrorCode.TOKEN_BUDGET_EXCEEDED
        # 预算耗尽是**控制决策**而非服务故障 ⇒ 应可降级为礼貌拒答，不是 5xx
        assert err.code in DEGRADABLE

    def test_clear_restores_inert_behaviour(self):
        """解绑后必须恢复 inert —— 防 ContextVar 跨请求「串台」。"""
        from agentix.application.services.token_budget import (
            BudgetLedger,
            budget_exhausted_error,
            clear_current_ledger,
            set_current_ledger,
        )

        ledger = BudgetLedger.from_settings(per_request=10)
        ledger.consume(100)
        set_current_ledger(ledger)
        assert budget_exhausted_error() is not None

        clear_current_ledger()
        assert budget_exhausted_error() is None

    def test_disabled_ledger_never_trips(self):
        """limit<=0（不限额）时无论消耗多少都不触发。"""
        from agentix.application.services.token_budget import (
            BudgetLedger,
            budget_exhausted_error,
            set_current_ledger,
        )

        ledger = BudgetLedger.from_settings(per_request=0)
        ledger.consume(10**9, agent="router")
        set_current_ledger(ledger)
        assert budget_exhausted_error("router") is None

    def test_tool_call_budget_blocks_after_limit(self):
        from agentix.application.services.token_budget import (
            BudgetLedger,
            budget_exhausted_error,
            set_current_ledger,
        )
        from agentix.domain.errors import AgentError, ErrorCode

        ledger = BudgetLedger.from_settings(per_request=0, max_tool_calls=2)
        assert ledger.record_tool_call() is True
        assert ledger.record_tool_call() is False
        set_current_ledger(ledger)

        err = budget_exhausted_error("router")
        assert isinstance(err, AgentError)
        assert err.code is ErrorCode.TOKEN_BUDGET_EXCEEDED


class TestControlPlaneLifecycle:
    def setup_method(self):
        self.plane = ControlPlane()

    def test_spawn_registers_pending_run(self):
        run = self.plane.spawn("kg_subgraph")
        assert run.state == RunState.PENDING
        assert self.plane.status(run.run_id) is run
        assert self.plane.active_count() == 1

    def test_mark_running_then_finish(self):
        run = self.plane.spawn("kg_subgraph")
        assert self.plane.mark_running(run.run_id) is True
        assert run.state == RunState.RUNNING
        assert run.started_at is not None
        assert self.plane.finish(run.run_id, state=RunState.SUCCEEDED) is True
        assert run.state == RunState.SUCCEEDED
        assert run.ended_at is not None
        assert self.plane.active_count() == 0

    def test_finish_is_idempotent_on_terminal(self):
        run = self.plane.spawn("x")
        self.plane.finish(run.run_id, state=RunState.SUCCEEDED)
        assert self.plane.finish(run.run_id, state=RunState.FAILED) is False
        assert run.state == RunState.SUCCEEDED

    def test_mark_running_rejects_terminal(self):
        run = self.plane.spawn("x")
        self.plane.finish(run.run_id, state=RunState.FAILED, error="boom")
        assert self.plane.mark_running(run.run_id) is False

    def test_unknown_run_operations_are_safe(self):
        assert self.plane.status("nope") is None
        assert self.plane.mark_running("nope") is False
        assert self.plane.finish("nope") is False
        assert self.plane.bind("nope", None) is False

    def test_list_runs_filter_by_state(self):
        a = self.plane.spawn("a")
        self.plane.spawn("b")
        self.plane.finish(a.run_id, state=RunState.SUCCEEDED)
        assert len(self.plane.list_runs(state=RunState.PENDING)) == 1
        assert len(self.plane.list_runs(state=RunState.SUCCEEDED)) == 1
        assert len(self.plane.list_runs()) == 2

    def test_run_carries_budget(self):
        budget = TokenBudget(limit=100)
        run = self.plane.spawn("a", budget=budget)
        assert run.to_dict()["budget"]["limit"] == 100

    def test_stats(self):
        run = self.plane.spawn("a")
        self.plane.finish(run.run_id)
        stats = self.plane.stats()
        assert stats["total"] == 1
        assert stats["active"] == 0
        assert stats["by_state"]["succeeded"] == 1


class TestControlPlaneCancel:
    def setup_method(self):
        self.plane = ControlPlane()

    @pytest.mark.asyncio
    async def test_cancel_marks_terminal(self):
        run = self.plane.spawn("x")
        assert await self.plane.cancel(run.run_id, reason="user") is True
        assert run.state == RunState.CANCELLED
        assert run.cancel_reason == "user"
        assert run.ended_at is not None

    @pytest.mark.asyncio
    async def test_cancel_is_idempotent(self):
        run = self.plane.spawn("x")
        await self.plane.cancel(run.run_id)
        assert await self.plane.cancel(run.run_id) is False

    @pytest.mark.asyncio
    async def test_cancel_actually_interrupts_bound_task(self):
        """取消必须真的打断底层 Task，而不是只改状态。"""
        cancelled = asyncio.Event()

        async def work():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        run = self.plane.spawn("x")
        task = asyncio.create_task(work())
        assert self.plane.bind(run.run_id, task) is True
        self.plane.mark_running(run.run_id)
        await asyncio.sleep(0)  # 让 task 起来
        await self.plane.cancel(run.run_id)
        assert cancelled.is_set()
        assert task.cancelled() or task.done()

    @pytest.mark.asyncio
    async def test_cancel_survives_task_exception(self):
        async def boom():
            raise RuntimeError("kaboom")

        run = self.plane.spawn("x")
        task = asyncio.create_task(boom())
        self.plane.bind(run.run_id, task)
        with pytest.raises(RuntimeError):
            await task
        assert await self.plane.cancel(run.run_id) is True
        assert run.state == RunState.CANCELLED


class TestControlPlaneBounds:
    def test_evicts_oldest_terminal_only(self):
        plane = ControlPlane(max_runs=2)
        a = plane.spawn("a")
        plane.finish(a.run_id)
        b = plane.spawn("b")
        plane.finish(b.run_id)
        plane.spawn("c")  # 超容 → 淘汰 a
        assert plane.status(a.run_id) is None
        assert plane.status(b.run_id) is not None

    def test_never_evicts_active_runs(self):
        """宁可超容，也不丢活动运行。"""
        plane = ControlPlane(max_runs=1)
        a = plane.spawn("a")
        plane.spawn("b")
        assert plane.status(a.run_id) is not None
        assert len(plane.list_runs()) == 2

    def test_clear(self):
        plane = ControlPlane()
        plane.spawn("a")
        plane.clear()
        assert plane.stats()["total"] == 0

    def test_reset_singleton(self):
        reset_control_plane()


# ---------------------------------------------------------------------------
# 目标优先级与冲突仲裁
# ---------------------------------------------------------------------------


class TestGoalArbitration:
    def test_precedence_order_is_complete(self):
        assert set(PRECEDENCE) == set(GoalKind)
        # 安全 < 合规 < 准确 < 有用 < 时延 < 成本（数值越小越优先）
        assert PRECEDENCE[GoalKind.SAFETY] < PRECEDENCE[GoalKind.COMPLIANCE]
        assert PRECEDENCE[GoalKind.COMPLIANCE] < PRECEDENCE[GoalKind.ACCURACY]
        assert PRECEDENCE[GoalKind.ACCURACY] < PRECEDENCE[GoalKind.HELPFULNESS]
        assert PRECEDENCE[GoalKind.HELPFULNESS] < PRECEDENCE[GoalKind.LATENCY]
        assert PRECEDENCE[GoalKind.LATENCY] < PRECEDENCE[GoalKind.COST]

    def test_orders_by_precedence(self):
        goals = [
            Goal(GoalKind.COST),
            Goal(GoalKind.SAFETY),
            Goal(GoalKind.HELPFULNESS),
        ]
        result = arbitrate(goals)
        assert [g.kind for g in result.ordered] == [
            GoalKind.SAFETY,
            GoalKind.HELPFULNESS,
            GoalKind.COST,
        ]
        assert result.dominant.kind == GoalKind.SAFETY

    def test_helpfulness_vs_compliance_compliance_wins(self):
        result = arbitrate([Goal(GoalKind.HELPFULNESS), Goal(GoalKind.COMPLIANCE)])
        assert result.dominant.kind == GoalKind.COMPLIANCE
        assert GoalKind.HELPFULNESS in [g.kind for g in result.vetoed]
        assert result.has_conflicts

    def test_accuracy_vs_latency_accuracy_wins(self):
        result = arbitrate([Goal(GoalKind.LATENCY), Goal(GoalKind.ACCURACY)])
        assert result.dominant.kind == GoalKind.ACCURACY

    def test_resolution_carries_human_readable_rule(self):
        result = arbitrate([Goal(GoalKind.COST), Goal(GoalKind.ACCURACY)])
        assert result.resolutions
        rule = result.resolutions[0].rule
        assert isinstance(rule, str) and len(rule) > 5

    def test_explicit_priority_cannot_cross_hard_precedence(self):
        """显式提权只在同层内生效——安全不可被配置让路。"""
        result = arbitrate(
            [
                Goal(GoalKind.SAFETY),
                Goal(GoalKind.COST, priority=0),  # 试图把成本提到最高
            ]
        )
        assert result.dominant.kind == GoalKind.SAFETY

    def test_explicit_priority_breaks_tie_within_layer(self):
        goals = [Goal(GoalKind.LATENCY), Goal(GoalKind.COST, priority=0)]
        result = arbitrate(goals)
        # COST 硬优先级最低，但显式提权到 0 后与 LATENCY(4) 交换
        assert result.dominant.kind == GoalKind.COST

    def test_duplicate_kinds_are_deduped_keeping_best(self):
        result = arbitrate([Goal(GoalKind.LATENCY), Goal(GoalKind.LATENCY, priority=1)])
        assert len(result.ordered) == 1
        assert result.ordered[0].priority == 1

    def test_determinism(self):
        goals = [Goal(GoalKind.COST), Goal(GoalKind.ACCURACY), Goal(GoalKind.LATENCY)]
        first = arbitrate(goals).to_dict()
        second = arbitrate(goals).to_dict()
        assert first == second

    def test_undefined_pair_is_reported(self):
        """未登记规则的目标对必须显式暴露，而不是静默通过。"""
        result = arbitrate([Goal(GoalKind.SAFETY), Goal(GoalKind.LATENCY)])
        assert ("safety", "latency") in result.undefined_pairs

    def test_all_conflict_pairs_have_rules(self):
        for pair, rule in CONFLICTING_PAIRS.items():
            assert len(pair) == 2
            assert isinstance(rule, str) and rule

    def test_empty_input(self):
        result = arbitrate([])
        assert result.ordered == []
        assert result.dominant is None
        assert dominant_goal([]) is None

    def test_dominant_goal_helper(self):
        assert dominant_goal([Goal(GoalKind.COST), Goal(GoalKind.SAFETY)]) == GoalKind.SAFETY

    def test_result_serialization(self):
        data = arbitrate([Goal(GoalKind.SAFETY), Goal(GoalKind.HELPFULNESS)]).to_dict()
        assert data["dominant"] == "safety"
        assert data["vetoed"]
        assert data["resolutions"]
