"""Stage F2c·A-P2-2（统一执行链）：Multi-Agent 统一退出契约 + 六失败模式打点。

钉死契约：

1. **词表单源**：``ManagerOutcome.exit_status/exit_reason`` 的值域必须等于
   ``domain/runtime/lifecycle.py`` 的 ``ExitStatus/ExitReason`` 值集
   （不另造平行词表——A-P0-3「先契约后功能」在 Multi-Agent 层的落地）；
2. **推导表**：``derive_exit_contract`` 对每类 ``reason`` 的
   ``(status, reason)`` 映射（converged/budget/cancelled/轮次耗尽/其余）；
3. **契约等式**：任何构造路径下 ``truncated == (exit_reason ==
   "max_iterations")``（``__post_init__`` 强制重算，禁止漂移）；
4. **打点**：``failure_mode != NONE`` ⇒ ``record_multiagent_failure(mode)``
   恰一次；``NONE`` 不计（成功收敛不进失败计数器）；
5. **supervisor 透传**：``additional_kwargs["multi_agent"]`` 携带
   ``status/exit_reason/truncated``；Manager 异常路径诚实
   ``failed/error``，不冒充完成态；
6. **评测面零接触**：``_aggregate_multi_agent`` 只读既有键——meta 新增键
   不改变聚合结果。
"""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agentix.domain.runtime.lifecycle import ExitReason, ExitStatus
from agentix.orchestration.langgraph import lg_supervisor
from agentix.orchestration.langgraph.lg_states import AgentState, Router
from agentix.orchestration.strategies.multi_agent import manager as manager_mod
from agentix.orchestration.strategies.multi_agent.manager import (
    FailureMode,
    Manager,
    ManagerConfig,
    ManagerOutcome,
    derive_exit_contract,
)

_EXIT_STATUS_VALUES = {s.value for s in ExitStatus}
_EXIT_REASON_VALUES = {s.value for s in ExitReason}


@pytest.fixture(autouse=True)
def _offline_no_llm(monkeypatch):
    monkeypatch.setattr(lg_supervisor, "_llm_available", lambda: False)


def _state(question: str = "违约金怎么算？", route_type: str = "general-query") -> AgentState:
    state = AgentState(messages=[HumanMessage(content=question)])
    state.router = Router(type=route_type, logic="test")
    return state


class _CountingHandler:
    def __init__(self, content: str = "这是子 agent 的草稿答案。"):
        self.calls = 0
        self.content = content

    async def __call__(self, state, *, config=None):
        self.calls += 1
        return {"messages": [AIMessage(content=self.content)], "sources": []}


# ---------------------------------------------------------------------------
# 1. 词表单源 + 推导表
# ---------------------------------------------------------------------------


class TestVocabularySingleSource:
    def test_status_values_equal_lifecycle_enum(self):
        assert _EXIT_STATUS_VALUES == {"completed", "interrupted", "failed"}

    def test_reason_values_equal_lifecycle_enum(self):
        assert _EXIT_REASON_VALUES == {"completed", "max_iterations", "interrupted", "error"}

    @pytest.mark.parametrize(
        "ok,reason,exp_status,exp_reason",
        [
            (True, "converged", "completed", "completed"),
            (False, "proposer_budget_exhausted", "failed", "max_iterations"),
            (False, "reviewer_budget_exhausted", "failed", "max_iterations"),
            (False, "max_rounds_exceeded", "failed", "max_iterations"),
            (False, "peer_cancelled:proposer", "interrupted", "interrupted"),
            (False, "peer_cancelled:reviewer", "interrupted", "interrupted"),
            (False, "no_progress", "failed", "error"),
            (False, "proposer_or_reviewer_not_wired", "failed", "error"),
            (False, "proposer_accept_reviewer_reject", "failed", "error"),
            (False, "contradiction_unresolved", "failed", "error"),
            (False, "depth 7 > max 3", "failed", "error"),
            (True, "", "completed", "completed"),
            (False, "", "failed", "error"),
        ],
    )
    def test_derivation_table(self, ok, reason, exp_status, exp_reason):
        assert derive_exit_contract(ok, reason) == (exp_status, exp_reason)

    @pytest.mark.parametrize("reason", [r for r in [
        "converged", "proposer_budget_exhausted", "max_rounds_exceeded",
        "peer_cancelled:proposer", "no_progress", "proposer_or_reviewer_not_wired",
        "contradiction_unresolved", "",
    ]])
    @pytest.mark.parametrize("ok", [True, False])
    def test_derived_vocab_in_enum(self, ok, reason):
        status, exp_reason = derive_exit_contract(ok, reason)
        assert status in _EXIT_STATUS_VALUES
        assert exp_reason in _EXIT_REASON_VALUES


# ---------------------------------------------------------------------------
# 2. ManagerOutcome 落章 + 契约等式 + 打点
# ---------------------------------------------------------------------------


def _outcome(
    *,
    ok: bool,
    reason: str = "",
    failure_mode: FailureMode = FailureMode.NONE,
    **kw,
) -> ManagerOutcome:
    return ManagerOutcome(
        ok=ok,
        final_output=kw.pop("final_output", "答案"),
        failure_mode=failure_mode,
        rounds=kw.pop("rounds", 1),
        reason=reason,
        **kw,
    )


class TestOutcomeStamping:
    def test_converged_completed(self):
        o = _outcome(ok=True, reason="converged")
        assert (o.exit_status, o.exit_reason, o.truncated) == (
            "completed",
            "completed",
            False,
        )

    @pytest.mark.parametrize(
        "reason",
        ["proposer_budget_exhausted", "reviewer_budget_exhausted", "max_rounds_exceeded"],
    )
    def test_budget_and_rounds_truncated(self, reason):
        o = _outcome(ok=False, reason=reason)
        assert o.exit_status == "failed"
        assert o.exit_reason == "max_iterations"
        assert o.truncated is True

    def test_cancel_interrupted(self):
        o = _outcome(ok=False, reason="peer_cancelled:proposer")
        assert (o.exit_status, o.exit_reason, o.truncated) == (
            "interrupted",
            "interrupted",
            False,
        )

    @pytest.mark.parametrize("ok", [True, False])
    @pytest.mark.parametrize(
        "reason",
        ["", "no_progress", "contradiction_unresolved", "proposer_or_reviewer_not_wired"],
    )
    def test_contract_identity_holds(self, ok, reason):
        o = _outcome(ok=ok, reason=reason)
        assert o.truncated == (o.exit_reason == ExitReason.MAX_ITERATIONS.value)
        assert o.exit_status in _EXIT_STATUS_VALUES
        assert o.exit_reason in _EXIT_REASON_VALUES

    def test_explicit_fields_not_overwritten(self):
        """已显式给 exit_* 的构造不被推导覆盖（等式仍强制重算 truncated）。"""
        o = ManagerOutcome(
            ok=False,
            final_output="",
            failure_mode=FailureMode.NONE,
            rounds=0,
            reason="whatever",
            exit_status="completed",
            exit_reason="max_iterations",
        )
        assert o.exit_status == "completed"
        assert o.exit_reason == "max_iterations"
        assert o.truncated is True  # 按等式重算


class TestFailureMetrics:
    @pytest.mark.asyncio
    async def test_failure_mode_recorded_once(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(manager_mod, "record_multiagent_failure", calls.append)

        _outcome(
            ok=False,
            reason="no_progress",
            failure_mode=FailureMode.DEADLOCK,
            rounds=3,
        )
        assert calls == ["deadlock"]

    @pytest.mark.asyncio
    async def test_none_not_recorded(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(manager_mod, "record_multiagent_failure", calls.append)

        _outcome(ok=True, reason="converged", failure_mode=FailureMode.NONE)
        assert calls == []

    @pytest.mark.asyncio
    async def test_escalation_path_records(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(manager_mod, "record_multiagent_failure", calls.append)

        mgr = Manager(config=ManagerConfig(max_escalation_depth=1))
        outcome = await mgr.escalate("目标", depth=5)
        assert outcome.failure_mode is FailureMode.INFINITE_ESCALATION
        assert calls == ["infinite_escalation"]
        assert outcome.exit_reason == "error"

    @pytest.mark.asyncio
    async def test_wired_not_wired_reason_maps_error(self):
        outcome = await Manager().run("目标")
        assert outcome.reason == "proposer_or_reviewer_not_wired"
        assert (outcome.exit_status, outcome.exit_reason) == ("failed", "error")

    def test_metrics_surface_exists(self):
        from agentix.observability import metrics

        assert callable(metrics.record_multiagent_failure)
        assert hasattr(metrics, "MULTIAGENT_FAILURE_MODE")


# ---------------------------------------------------------------------------
# 3. supervisor meta 透传
# ---------------------------------------------------------------------------


class TestSupervisorMetaContract:
    @pytest.mark.asyncio
    async def test_converge_meta_carries_exit_contract(self, monkeypatch):
        handler = _CountingHandler()
        monkeypatch.setitem(lg_supervisor.ROUTE_HANDLERS, "respond_to_general_query", handler)

        result = await lg_supervisor.supervisor_dispatch(_state(), config={})
        meta = result["messages"][-1].additional_kwargs["multi_agent"]
        assert meta["status"] == "completed"
        assert meta["exit_reason"] == "completed"
        assert meta["truncated"] is False

    @pytest.mark.asyncio
    async def test_unresolved_meta_failed_error(self, monkeypatch):
        handler = _CountingHandler(content="")
        monkeypatch.setitem(lg_supervisor.ROUTE_HANDLERS, "respond_to_general_query", handler)

        result = await lg_supervisor.supervisor_dispatch(_state(), config={})
        meta = result["messages"][-1].additional_kwargs["multi_agent"]
        assert meta["failure_mode"] == "contradiction"
        assert meta["status"] == "failed"
        assert meta["exit_reason"] == "error"
        assert meta["truncated"] is False

    @pytest.mark.asyncio
    async def test_manager_exception_honest_failed_error(self, monkeypatch):
        handler = _CountingHandler()
        monkeypatch.setitem(lg_supervisor.ROUTE_HANDLERS, "respond_to_general_query", handler)

        class _BoomManager:
            def __init__(self, **kwargs):
                pass

            async def run(self, goal, **kwargs):
                raise RuntimeError("boom")

        monkeypatch.setattr(lg_supervisor, "Manager", _BoomManager)
        result = await lg_supervisor.supervisor_dispatch(_state(), config={})
        meta = result["messages"][-1].additional_kwargs["multi_agent"]
        assert meta["failure_mode"] == "manager_exception"
        assert meta["status"] == "failed"
        assert meta["exit_reason"] == "error"
        assert meta["truncated"] is False


# ---------------------------------------------------------------------------
# 4. 评测面零接触（聚合只读既有键）
# ---------------------------------------------------------------------------


class TestEvalAggregationUnaffected:
    def test_aggregate_reads_only_known_keys(self):
        from evals.run_eval import _aggregate_multi_agent

        base = {
            "ok": True,
            "rounds": 1,
            "failure_mode": "none",
            "incremental_ratio": 1.0,
        }
        enriched = {
            **base,
            "status": "completed",
            "exit_reason": "completed",
            "truncated": False,
        }
        agg_plain = _aggregate_multi_agent([{"multi_agent": dict(base)}])
        agg_new = _aggregate_multi_agent([{"multi_agent": dict(enriched)}])
        assert agg_plain == agg_new
        assert agg_new["multi_agent_ok_rate"] == 1.0


def test_supervisor_doc_mentions_contract_keys():
    """文档面：supervisor docstring 声明三键（防回退不透明）。"""
    import inspect

    text = inspect.getdoc(lg_supervisor.supervisor_dispatch) or ""
    for key in ("status", "exit_reason", "truncated"):
        assert key in text
