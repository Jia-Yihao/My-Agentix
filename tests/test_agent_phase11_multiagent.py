"""Phase 11 — Multi-Agent：信息增量、六失败模式、独立预算、Peer+Manager。"""

from __future__ import annotations

import pytest

from agentix.orchestration.strategies.multi_agent import (
    AgentRole,
    FailureMode,
    Manager,
    ManagerConfig,
    PeerBudget,
    PeerMessage,
    PeerResult,
    detect_failure_mode,
    has_information_increment,
    information_increment,
    token_overlap_ratio,
)


class TestInformationIncrement:
    def test_new_content_is_incremental(self):
        r = information_increment("构成要件包括主体客体主观方面", "合同成立生效")
        assert r["incremental"] is True
        assert r["new_token_count"] > 0

    def test_empty_candidate_not_incremental(self):
        r = information_increment("", "已有上下文")
        assert r["incremental"] is False
        assert r["reason"] == "no_new_tokens"

    def test_near_duplicate_not_incremental(self):
        base = "民法典合同编违约责任继续履行赔偿损失" * 3
        r = information_increment(base, base)
        assert r["incremental"] is False

    def test_high_overlap_echo_detected(self):
        text = "alpha beta gamma delta epsilon zeta"
        r = information_increment(text + "!", text, max_overlap=0.5)
        assert r["incremental"] is False
        assert r["reason"] in ("high_overlap_echo", "no_new_tokens")

    def test_overlap_ratio_bounds(self):
        assert token_overlap_ratio("", "x") == 0.0
        assert token_overlap_ratio("abc", "abc") == 1.0
        assert has_information_increment("全新的证据链事实", "旧结论")


class TestSixFailureModes:
    def test_none(self):
        assert detect_failure_mode() is FailureMode.NONE

    def test_deadlock_by_no_progress(self):
        assert (
            detect_failure_mode(rounds_no_progress=3) is FailureMode.DEADLOCK
        )

    def test_livelock_zero_increment(self):
        assert (
            detect_failure_mode(messages_with_zero_increment=3, echo_overlap=0.5)
            is FailureMode.LIVELOCK
        )

    def test_echo_chamber_high_overlap(self):
        assert (
            detect_failure_mode(messages_with_zero_increment=3, echo_overlap=0.95)
            is FailureMode.ECHO_CHAMBER
        )
        assert detect_failure_mode(echo_overlap=0.96) is FailureMode.ECHO_CHAMBER

    def test_budget_bleed(self):
        assert detect_failure_mode(peer_budget_exhausted=True) is FailureMode.BUDGET_BLEED

    def test_infinite_escalation(self):
        assert (
            detect_failure_mode(escalation_depth=5, max_escalation_depth=3)
            is FailureMode.INFINITE_ESCALATION
        )

    def test_contradiction_priority(self):
        mode = detect_failure_mode(
            contradiction=True, peer_budget_exhausted=True, rounds_no_progress=5
        )
        assert mode is FailureMode.CONTRADICTION

    def test_all_six_modes_exist(self):
        values = {m.value for m in FailureMode}
        assert values == {
            "none",
            "deadlock",
            "livelock",
            "echo_chamber",
            "budget_bleed",
            "infinite_escalation",
            "contradiction",
        }


class TestIndependentBudget:
    def test_budget_isolation_per_peer(self):
        mgr = Manager()
        mgr.budget_for("proposer").max_steps = 2
        assert mgr.budget_for("reviewer").max_steps == 10  # 默认独立
        assert mgr.budget_for("proposer").max_steps == 2

    def test_exhausted_steps(self):
        b = PeerBudget(max_steps=2)
        assert b.exhausted_steps(0) is False
        assert b.exhausted_steps(2) is True

    @pytest.mark.asyncio
    async def test_manager_stops_on_peer_budget(self):
        budgets = {
            "proposer": PeerBudget(max_steps=1, max_messages=1),
            "reviewer": PeerBudget(max_steps=10, max_messages=10),
        }

        async def prop(**kw):
            return PeerResult(
                agent_id="proposer",
                role=AgentRole.PROPOSER,
                output="reject 需要重做",
                meta={"decision": "reject"},
            )

        async def rev(**kw):
            return PeerResult(
                agent_id="reviewer",
                role=AgentRole.REVIEWER,
                output="reject 同意重做",
                meta={"decision": "reject"},
            )

        mgr = Manager(budgets=budgets, proposer=prop, reviewer=rev)
        out = await mgr.run("goal")
        assert out.ok is False
        assert out.failure_mode in (FailureMode.BUDGET_BLEED, FailureMode.DEADLOCK)


class TestPeerMessageIsolation:
    def test_peer_message_public_dict_has_no_trajectory(self):
        msg = PeerMessage(
            sender="a",
            receiver="b",
            role=AgentRole.PROPOSER,
            content="结论",
        )
        pub = msg.to_public_dict()
        for banned in ("trajectory", "prompt", "messages", "history"):
            assert banned not in pub


class TestManagerConvergence:
    @pytest.mark.asyncio
    async def test_converge_on_accept_approve(self):
        async def prop(**kw):
            return PeerResult(
                agent_id="p",
                role=AgentRole.PROPOSER,
                output="最终答案已完备 accept_final",
                meta={"decision": "accept_final"},
            )

        async def rev(**kw):
            return PeerResult(
                agent_id="r",
                role=AgentRole.REVIEWER,
                output="结构与证据齐全 approve",
                meta={"decision": "approve"},
            )

        # 第二轮内容需有增量否则第三轮可能被丢——首轮即收敛
        mgr = Manager(proposer=prop, reviewer=rev)
        out = await mgr.run("证明违约")
        assert out.ok is True
        assert out.failure_mode is FailureMode.NONE
        assert out.rounds == 1

    @pytest.mark.asyncio
    async def test_contradiction_detected(self):
        async def prop(**kw):
            return PeerResult(
                agent_id="p",
                role=AgentRole.PROPOSER,
                output="accept 放行",
                meta={"decision": "accept"},
            )

        async def rev(**kw):
            return PeerResult(
                agent_id="r",
                role=AgentRole.REVIEWER,
                output="reject 证据不足",
                meta={"decision": "reject"},
            )

        mgr = Manager(proposer=prop, reviewer=rev)
        out = await mgr.run("g")
        assert out.ok is False
        assert out.failure_mode is FailureMode.CONTRADICTION

    @pytest.mark.asyncio
    async def test_escalation_depth_limit(self):
        mgr = Manager(config=ManagerConfig(max_escalation_depth=1))
        out = await mgr.escalate("g", depth=3)
        assert out.failure_mode is FailureMode.INFINITE_ESCALATION
        assert out.ok is False

    @pytest.mark.asyncio
    async def test_missing_wiring_fails_clean(self):
        mgr = Manager()
        out = await mgr.run("g")
        assert out.ok is False
        assert out.reason == "proposer_or_reviewer_not_wired"

    @pytest.mark.asyncio
    async def test_unresolved_contradiction_reported_at_max_rounds(self):
        """stop_on_contradiction=False 时，终态仍须上报 contradiction（不丢标记）。"""

        async def prop(**kw):
            return PeerResult(
                agent_id="p",
                role=AgentRole.PROPOSER,
                output="accept 放行",
                meta={"decision": "accept"},
            )

        async def rev(**kw):
            return PeerResult(
                agent_id="r",
                role=AgentRole.REVIEWER,
                output="reject 证据不足",
                meta={"decision": "reject"},
            )

        mgr = Manager(
            config=ManagerConfig(max_rounds=2, stop_on_contradiction=False),
            proposer=prop,
            reviewer=rev,
        )
        out = await mgr.run("g")
        assert out.ok is False
        assert out.failure_mode is FailureMode.CONTRADICTION
        assert out.reason == "contradiction_unresolved"
        assert out.rounds == 2
