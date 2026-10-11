"""Phase 25 — multi-agent 运行时防线（Ch10 §10.17 六失败模式 + 控制平面准入）。

覆盖（对齐 docs/migration/Phase21-26_规划.md Phase 25 DoD）：

1. 六失败模式**运行时防线**（离线 fake peer，不依赖 LLM/Redis/Docker）：
   deadlock / livelock / echo_chamber / budget_bleed / infinite_escalation /
   contradiction —— 经 ``Manager.run`` 真实路径，不只测检测函数。
2. 控制平面准入：``CONTROL_PLANE_MAX_CONCURRENCY`` / ``CONTROL_PLANE_MAX_DEPTH``
   （0 = 不限 = 接线前行为；超限 ``AdmissionDenied`` fail-closed，
   ``_invoke_tracked`` 不得静默降级）。
3. 进程内消息总线 ``InProcessBus``：discover/send/recv/status/cancel 最小原语 +
   有界队列 + peer 粒度取消；flag-off（bus=None）直连行为逐位等价。
4. 逻辑级冲突**只标记不转述**（artifact 同 key 不同值 / 结论极性相反 →
   ``ManagerOutcome.meta.conflicts``；final_output 不合并不传播）。
5. 预算单源：settings 与 ``PeerBudget`` 默认值镜像（防漂移）。
"""

from __future__ import annotations

import inspect

import pytest

from agentix.orchestration.strategies.multi_agent import (
    AgentRole,
    InProcessBus,
    Manager,
    ManagerConfig,
    PeerBudget,
    PeerMessage,
    PeerResult,
    FailureMode,
    detect_artifact_conflicts,
)
from agentix.runtime.control_plane import (
    AdmissionDenied,
    ControlPlane,
    reset_control_plane,
)


# ---------------------------------------------------------------------------
# fake peers
# ---------------------------------------------------------------------------


def _proposer(output: str, decision: str, *, artifacts=None):
    meta = {"decision": decision}
    if artifacts is not None:
        meta["artifacts"] = artifacts

    async def _fn(**kwargs):
        return PeerResult(
            agent_id="proposer", role=AgentRole.PROPOSER, output=output, meta=dict(meta)
        )

    return _fn


def _reviewer(output: str, decision: str, *, artifacts=None):
    meta = {"decision": decision}
    if artifacts is not None:
        meta["artifacts"] = artifacts

    async def _fn(**kwargs):
        return PeerResult(
            agent_id="reviewer", role=AgentRole.REVIEWER, output=output, meta=dict(meta)
        )

    return _fn


# ---------------------------------------------------------------------------
# 1. 六失败模式：运行时防线（Manager 真实路径）
# ---------------------------------------------------------------------------


class TestSixFailureModesRuntime:
    @pytest.mark.asyncio
    async def test_deadlock_repeated_rejection_stops(self):
        """模式一 deadlock：双方反复拒绝、零进展 → 3 轮后 DEADLOCK 停机。"""
        mgr = Manager(
            config=ManagerConfig(max_rounds=6),
            proposer=_proposer("提案无法推进", "reject"),
            reviewer=_reviewer("仍然不行，理由不同", "reject"),
        )
        outcome = await mgr.run("测试目标")
        assert outcome.ok is False
        assert outcome.failure_mode is FailureMode.DEADLOCK
        assert outcome.reason == "no_progress"
        assert outcome.rounds < 6, "防线必须提前停机，不能烧满预算"

    @pytest.mark.asyncio
    async def test_livelock_zero_increment_messages(self):
        """模式二 livelock：消息在转但信息增量恒 0 → 轮次耗尽判 LIVELOCK。"""
        proposals = iter(["相同提案内容不变"] * 10)

        async def prop(**kwargs):
            return PeerResult(
                agent_id="proposer",
                role=AgentRole.PROPOSER,
                output=next(proposals),
                meta={"decision": "ok"},  # ok ≠ accept：不收敛也不矛盾
            )

        mgr = Manager(
            config=ManagerConfig(max_rounds=4),
            proposer=prop,
            reviewer=_reviewer("复核记录：继续观察进展", "approve"),
        )
        outcome = await mgr.run("测试目标")
        assert outcome.ok is False
        assert outcome.failure_mode is FailureMode.LIVELOCK
        assert outcome.rounds == 4

    @pytest.mark.asyncio
    async def test_echo_chamber_high_overlap(self):
        """模式三 echo_chamber：Reviewer 复读 Proposer（overlap≈1）→ 判 ECHO。"""
        proposals = iter(["方案甲内容一", "方案乙内容二", "方案丙内容三", "方案丁内容四"])

        async def prop(**kwargs):
            return PeerResult(
                agent_id="proposer",
                role=AgentRole.PROPOSER,
                output=next(proposals),
                meta={"decision": "ok"},
            )

        async def rev(*, proposal, **kwargs):
            return PeerResult(
                agent_id="reviewer",
                role=AgentRole.REVIEWER,
                output=proposal,  # 原样复读
                meta={"decision": "approve"},
            )

        mgr = Manager(config=ManagerConfig(max_rounds=4), proposer=prop, reviewer=rev)
        outcome = await mgr.run("测试目标")
        assert outcome.ok is False
        assert outcome.failure_mode is FailureMode.ECHO_CHAMBER

    @pytest.mark.asyncio
    async def test_budget_bleed_via_budget_factory(self):
        """模式四 budget_bleed：budget_factory 注入 1 步预算 → 首轮即判。"""

        def factory(agent_id: str) -> PeerBudget:
            return PeerBudget(max_steps=1, max_messages=1)

        mgr = Manager(
            config=ManagerConfig(max_rounds=6),
            proposer=_proposer("提案", "accept"),
            reviewer=_reviewer("approve", "approve"),
            budget_factory=factory,
        )
        outcome = await mgr.run("测试目标")
        assert outcome.ok is False
        assert outcome.failure_mode is FailureMode.BUDGET_BLEED
        assert outcome.reason == "proposer_budget_exhausted"
        assert mgr.budget_for("proposer").max_steps == 1

    @pytest.mark.asyncio
    async def test_infinite_escalation_depth_guard(self):
        """模式五 infinite_escalation：再委托深度超限 → 拒绝启动。"""
        mgr = Manager(
            config=ManagerConfig(max_escalation_depth=3),
            proposer=_proposer("提案", "accept"),
            reviewer=_reviewer("approve", "approve"),
        )
        outcome = await mgr.escalate("更深的目标", depth=5)
        assert outcome.ok is False
        assert outcome.failure_mode is FailureMode.INFINITE_ESCALATION
        assert "depth 5" in outcome.reason

    @pytest.mark.asyncio
    async def test_contradiction_proposer_accept_reviewer_reject(self):
        """模式六 contradiction：accept vs reject → CONTRADICTION + 极性标记。"""
        mgr = Manager(
            config=ManagerConfig(max_rounds=6, stop_on_contradiction=True),
            proposer=_proposer("应当认定违约", "accept"),
            reviewer=_reviewer("reject: 证据不足", "reject"),
        )
        outcome = await mgr.run("测试目标")
        assert outcome.ok is False
        assert outcome.failure_mode is FailureMode.CONTRADICTION
        assert outcome.reason == "proposer_accept_reviewer_reject"
        # 极性相反进 meta（只标记）
        assert outcome.meta["conflict_marked"] is True
        assert any(c["type"] == "polarity" for c in outcome.meta["conflicts"])


# ---------------------------------------------------------------------------
# 2. 逻辑级冲突：只标记不转述（反级联）
# ---------------------------------------------------------------------------


class TestConflictMarking:
    def test_artifact_same_key_different_value(self):
        results = [
            PeerResult(
                agent_id="a",
                role=AgentRole.PROPOSER,
                output="甲",
                meta={"decision": "ok", "artifacts": {"liability": "违约金上限 30%"}},
            ),
            PeerResult(
                agent_id="b",
                role=AgentRole.REVIEWER,
                output="乙",
                meta={"decision": "approve", "artifacts": {"liability": "违约金上限 50%"}},
            ),
        ]
        conflicts = detect_artifact_conflicts(results)
        assert len(conflicts) == 1
        entry = conflicts[0]
        assert entry["type"] == "artifact"
        assert entry["key"] == "liability"
        assert {v["value"] for v in entry["values"]} == {"违约金上限 30%", "违约金上限 50%"}

    def test_no_conflict_when_values_agree(self):
        results = [
            PeerResult(
                agent_id="a",
                role=AgentRole.PROPOSER,
                output="甲",
                meta={"decision": "ok", "artifacts": {"k": "v"}},
            ),
            PeerResult(
                agent_id="b",
                role=AgentRole.REVIEWER,
                output="乙",
                meta={"decision": "approve", "artifacts": {"k": "v"}},
            ),
        ]
        assert detect_artifact_conflicts(results) == []

    @pytest.mark.asyncio
    async def test_conflict_is_marked_not_merged_into_output(self):
        """冲突值不合并不转述：final_output 仍是提案原文，冲突只进 meta。"""
        mgr = Manager(
            config=ManagerConfig(max_rounds=3),
            proposer=_proposer(
                "提案正文：违约金上限 30%",
                "accept",
                artifacts={"liability": "违约金上限 30%"},
            ),
            reviewer=_reviewer(
                "approve: 通过",
                "approve",
                artifacts={"liability": "违约金上限 50%"},
            ),
        )
        outcome = await mgr.run("目标")
        assert outcome.ok is True
        assert outcome.final_output == "提案正文：违约金上限 30%"
        assert "50%" not in outcome.final_output, "冲突值不得沿链转述"
        assert outcome.meta["conflict_marked"] is True
        assert any(c["type"] == "artifact" for c in outcome.meta["conflicts"])


# ---------------------------------------------------------------------------
# 3. 进程内消息总线（最小原语 + 有界 + peer 粒度取消）
# ---------------------------------------------------------------------------


class TestInProcessBus:
    @pytest.mark.asyncio
    async def test_send_recv_discover_status(self):
        bus = InProcessBus()
        bus.subscribe("proposer")
        bus.subscribe("reviewer")
        assert bus.discover() == ["proposer", "reviewer"]

        msg = PeerMessage(
            sender="proposer", receiver="reviewer", role=AgentRole.PROPOSER, content="你好"
        )
        assert bus.send(msg) is True
        got = await bus.recv("reviewer", timeout=1.0)
        assert got.content == "你好"

        status = bus.status()
        assert status["subscribers"] == 2
        assert status["sent"] == 1
        assert bus.status("reviewer")["queue_size"] == 0

    def test_unknown_receiver_is_dropped_not_raised(self):
        bus = InProcessBus()
        msg = PeerMessage(
            sender="a", receiver="ghost", role=AgentRole.PROPOSER, content="x"
        )
        assert bus.send(msg) is False
        assert bus.stats()["dropped"] == 1

    def test_bounded_queue_drops_when_full(self):
        bus = InProcessBus(maxsize=1)
        bus.subscribe("r")
        first = PeerMessage(sender="a", receiver="r", role=AgentRole.PROPOSER, content="1")
        second = PeerMessage(sender="a", receiver="r", role=AgentRole.PROPOSER, content="2")
        assert bus.send(first) is True
        assert bus.send(second) is False, "有界队列满必须丢弃并计数，不得无界膨胀"
        assert bus.stats() == {
            "sent": 1,
            "dropped": 1,
            "cancelled_in_flight": 0,
            "cancel_calls": 0,
        }

    @pytest.mark.asyncio
    async def test_cancel_drains_inflight_and_refuses_new(self):
        bus = InProcessBus()
        bus.subscribe("p")
        msg = PeerMessage(sender="m", receiver="p", role=AgentRole.MANAGER, content="在途")
        assert bus.send(msg) is True

        drained = bus.cancel("p")
        assert drained == 1, "peer 取消必须清空在途消息"
        assert bus.is_cancelled("p") is True
        assert bus.send(msg) is False, "已取消 peer 拒收新消息"
        assert bus.status("p")["cancelled"] is True
        assert bus.status()["cancelled_in_flight"] == 1

        assert bus.resume("p") is True
        assert bus.is_cancelled("p") is False
        assert bus.send(msg) is True

    @pytest.mark.asyncio
    async def test_recv_unknown_agent_raises_keyerror(self):
        bus = InProcessBus()
        with pytest.raises(KeyError):
            await bus.recv("nobody", timeout=0.1)


# ---------------------------------------------------------------------------
# 4. Manager × bus：flag-off 等价 / flag-on 发布与 peer 取消
# ---------------------------------------------------------------------------


class TestManagerBusInjection:
    @pytest.mark.asyncio
    async def test_flag_off_no_bus_in_meta_and_direct_path(self):
        mgr = Manager(
            config=ManagerConfig(max_rounds=3),
            proposer=_proposer("提案", "accept"),
            reviewer=_reviewer("approve", "approve"),
        )
        outcome = await mgr.run("目标")
        assert outcome.ok is True
        assert "bus" not in outcome.meta, "flag-off（bus=None）不得出现 bus 观测键"
        assert outcome.meta["conflicts"] == []
        assert mgr.budget_for("proposer") == PeerBudget(), "无 factory 时保持默认预算"

    @pytest.mark.asyncio
    async def test_flag_on_messages_published_through_bus(self):
        bus = InProcessBus()
        mgr = Manager(
            config=ManagerConfig(max_rounds=3),
            proposer=_proposer("提案正文有新信息", "accept"),
            reviewer=_reviewer("approve: 通过", "approve"),
            bus=bus,
        )
        outcome = await mgr.run("目标")
        assert outcome.ok is True
        assert outcome.meta["bus"]["sent"] == 2, "Proposer/Reviewer 各发布 1 条"
        assert outcome.meta["bus"]["subscribers"] >= 0
        assert mgr.messages, "本地审计记录不受 bus 影响"

    @pytest.mark.asyncio
    async def test_peer_granular_cancel_stops_run(self):
        bus = InProcessBus()
        bus.subscribe("proposer")
        called = {"proposer": 0}

        async def prop(**kwargs):
            called["proposer"] += 1
            return PeerResult(
                agent_id="proposer", role=AgentRole.PROPOSER, output="提案", meta={"decision": "accept"}
            )

        mgr = Manager(
            config=ManagerConfig(max_rounds=3),
            proposer=prop,
            reviewer=_reviewer("approve", "approve"),
            bus=bus,
        )
        bus.cancel("proposer")
        outcome = await mgr.run("目标")
        assert outcome.ok is False
        assert outcome.reason == "peer_cancelled:proposer"
        assert called["proposer"] == 0, "被取消的 peer 不得被调用（取消 fan-out）"


# ---------------------------------------------------------------------------
# 5. 控制平面准入（concurrency / depth，0 = 不限）
# ---------------------------------------------------------------------------


class TestControlPlaneAdmission:
    def test_default_unlimited_matches_pre_wiring_behavior(self):
        plane = ControlPlane()
        plane.check_admission(depth=999)  # 默认 0 = 不限，永不拒
        run = plane.spawn("agent", depth=7)
        assert run.meta["depth"] == 7
        assert plane.max_concurrency == 0
        assert plane.max_depth == 0

    def test_concurrency_admission_denies_when_full(self):
        plane = ControlPlane(max_concurrency=1)
        first = plane.spawn("a")
        with pytest.raises(AdmissionDenied):
            plane.spawn("b")
        assert plane.active_count() == 1, "拒绝的运行不得登记"

        plane.finish(first.run_id)
        second = plane.spawn("b")  # 有空位后放行
        assert second.state.value == "pending"

    def test_depth_admission(self):
        plane = ControlPlane(max_depth=2)
        plane.spawn("ok", depth=2)
        with pytest.raises(AdmissionDenied):
            plane.spawn("too-deep", depth=3)

    @pytest.mark.asyncio
    async def test_invoke_tracked_admission_denied_fail_closed(self, monkeypatch):
        """_invoke_tracked：准入拒绝必须 fail-closed 上抛，且关闭协程防泄漏。"""
        from agentix.application.services import chat_service
        from agentix.runtime import control_plane as cp_mod

        class _FullPlane:
            def spawn(self, agent_name, **kwargs):
                raise AdmissionDenied("admission denied: full")

        monkeypatch.setattr(cp_mod, "get_control_plane", lambda: _FullPlane())

        async def _coro():
            return "never runs"

        coro = _coro()
        with pytest.raises(AdmissionDenied):
            await chat_service._invoke_tracked(coro)
        assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED, (
            "拒绝路径必须关闭协程，否则泄漏 never-awaited 警告"
        )

    @pytest.mark.asyncio
    async def test_invoke_tracked_registry_failure_still_degrades(self, monkeypatch):
        """登记表**故障**（非策略拒绝）仍降级直跑——两个语义不能混。"""
        from agentix.application.services import chat_service
        from agentix.runtime import control_plane as cp_mod

        class _BrokenPlane:
            def spawn(self, agent_name, **kwargs):
                raise RuntimeError("registry corrupt")

        monkeypatch.setattr(cp_mod, "get_control_plane", lambda: _BrokenPlane())

        async def _coro():
            return "ran directly"

        assert await chat_service._invoke_tracked(_coro()) == "ran directly"

    def test_singleton_reads_settings_limits(self, monkeypatch):
        from agentix.config import settings as app_settings

        reset_control_plane()
        monkeypatch.setattr(app_settings, "CONTROL_PLANE_MAX_CONCURRENCY", 2)
        monkeypatch.setattr(app_settings, "CONTROL_PLANE_MAX_DEPTH", 4)
        try:
            from agentix.runtime.control_plane import get_control_plane

            plane = get_control_plane()
            assert plane.max_concurrency == 2
            assert plane.max_depth == 4
            with pytest.raises(AdmissionDenied):
                plane.spawn("x", depth=5)
        finally:
            reset_control_plane()


# ---------------------------------------------------------------------------
# 6. 预算单源：settings 与 PeerBudget 默认镜像（防漂移）
# ---------------------------------------------------------------------------


class TestBudgetSettingsMirror:
    def test_settings_defaults_match_peer_budget_defaults(self):
        from agentix.config import settings as app_settings
        from agentix.orchestration.langgraph.lg_supervisor import _budget_factory

        defaults = PeerBudget()
        assert int(app_settings.MULTIAGENT_PEER_MAX_STEPS) == defaults.max_steps
        assert int(app_settings.MULTIAGENT_PEER_MAX_MESSAGES) == defaults.max_messages
        # 组装点产出与无注入时逐位一致（flag-off 等价）
        assert _budget_factory("proposer") == defaults

    def test_budget_factory_honors_changed_settings(self, monkeypatch):
        from agentix.config import settings as app_settings
        from agentix.orchestration.langgraph.lg_supervisor import _budget_factory

        monkeypatch.setattr(app_settings, "MULTIAGENT_PEER_MAX_STEPS", 3)
        monkeypatch.setattr(app_settings, "MULTIAGENT_PEER_MAX_MESSAGES", 5)
        budget = _budget_factory("reviewer")
        assert budget.max_steps == 3
        assert budget.max_messages == 5

    def test_bus_flag_defaults_off_and_gates_construction(self, monkeypatch):
        from agentix.config import settings as app_settings
        from agentix.orchestration.langgraph.lg_supervisor import _make_bus

        assert app_settings.MULTIAGENT_BUS_ENABLED is False, "总线 flag 必须默认关"
        assert _make_bus() is None, "flag-off = 不构造 bus（直连现行为）"

        monkeypatch.setattr(app_settings, "MULTIAGENT_BUS_ENABLED", True)
        bus = _make_bus()
        assert isinstance(bus, InProcessBus)
