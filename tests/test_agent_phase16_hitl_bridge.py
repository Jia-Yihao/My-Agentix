"""Phase 16：HITL 三栈库级桥（Gate → Sidecar → WAITING_APPROVAL/breakpoint）。

把规划 §5 的链路与 DoD 逐条钉死：

1. **默认关**：flag 关 / hook=None ⇒ executor 零变化（钩子永不触发）；
2. **开启 fail-closed**：拦下必挂 Sidecar；approver 否决 ⇒ REJECT 落回闸门；
3. **EDIT×approver / 重启恢复 E2E**：编辑合并 + digest/risk 同式重算；
   ``to_persistence`` → ``from_persistence`` 后裁决仍能落闸门；
4. **误拒可观测**：``stats()`` 给出拦截数、裁决分布与 ``rejection_rate``。

附带：permission_mode 三模式（default / accept_edits / bypass）、
breakpoint best-effort、状态机挂起与恢复。

工具声明只用 L0/L2（项目治理：``ToolSpec`` 禁止注册 L3），CRITICAL 级
拦截由**名称模式**（delete→CRITICAL）或**参数提权**（force）触发。
"""

from __future__ import annotations

import pytest

from agentix.policies.hitl.sidecar import ReviewDecisionAction, ReviewerSidecar
from agentix.policies.hitl.hitl_bridge import (
    BridgeDecision,
    HitlBridge,
    PermissionMode,
    get_hitl_bridge,
    install_executor_hook,
    reset_hitl_bridge,
)
from agentix.tools.definitions import ToolSpec, registry as tool_registry
from agentix.tools.gateway.executor import ToolExecutor, get_tool_executor, reset_tool_executor
from agentix.tools.gateway.human_review import (
    HumanReviewGate,
    ReviewDecision,
    ReviewRequired,
    RiskLevel,
    declared_level_for,
)
from agentix.runtime.lifecycle import LifecycleManager
from agentix.runtime.state import AgentState, AgentStatus


class _Policy:
    def __init__(self, max_risk_level: str = "L3") -> None:
        self.max_risk_level = max_risk_level


class _Spy:
    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, args):
        self.calls.append(dict(args))
        return {"ok": True}


class _FakeBreakpoints:
    """BreakpointManager 的离线替身（记录 + 可注入故障）。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.interrupts: list = []
        self.resolved: list = []
        self._fail = fail

    async def record_interrupt(self, record) -> None:
        if self._fail:
            raise RuntimeError("redis down")
        self.interrupts.append(record)

    async def resolve_interrupt(self, interrupt_id, action, *, override_data=None) -> None:
        self.resolved.append((interrupt_id, action.value, override_data))


@pytest.fixture
def registered_tool():
    created: list[str] = []

    def _register(name: str, *, risk_level: str, side_effect: bool = True) -> ToolSpec:
        spec = ToolSpec(
            name=name,
            description=f"test tool {name}",
            risk_level=risk_level,
            # L2+ 必须声明控制措施（p2.1.5 治理校验）
            control="人工审批（HITL）" if risk_level in {"L1", "L2", "L3"} else "",
            side_effect=side_effect,
        )
        created.append(name)
        return spec

    yield _register

    for name in created:
        tool_registry.delete_tool(name)


def _make_stack(*, mode: PermissionMode = PermissionMode.MANUAL, sidecar=None, breakpoints=None):
    gate = HumanReviewGate(enabled=True)
    sidecar = sidecar or ReviewerSidecar()
    bridge = HitlBridge(
        gate=gate, sidecar=sidecar, breakpoints=breakpoints, permission_mode=mode
    )
    return gate, sidecar, bridge


def _make_executor(gate, bridge) -> ToolExecutor:
    ex = ToolExecutor(policy=_Policy(), review_gate=gate)
    if bridge is not None:
        ex.set_review_blocked_hook(bridge.open_from_gate)
    return ex


async def _block(ex, name, args) -> tuple:
    """执行一次并返回 ``(result, request_id)``（result 必须是 review_required）。"""
    result = await ex.execute(name, args)
    assert not result.success
    assert result.metadata.get("reason") == "review_required"
    return result, result.metadata["request_id"]


def _blocked_request(gate, name, args, spec):
    """不经 executor 直接触发闸门拦截（避免钩子路径造成双开挂起）。"""
    with pytest.raises(ReviewRequired) as ei:
        gate.preflight(name, args, declared_level=declared_level_for(spec.risk_level))
    return ei.value.request


# ---------------------------------------------------------------------------
# 1. 默认关：flag 关 / hook=None ⇒ 零变化
# ---------------------------------------------------------------------------


class TestFlagOffZeroChange:
    @pytest.mark.asyncio
    async def test_flag_off_passes_through_and_hook_never_called(self, registered_tool):
        spec = registered_tool("delete_records", risk_level="L2")
        gate = HumanReviewGate(enabled=False)  # flag 关
        called = []

        async def hook(request, args):
            called.append(request)
            return None

        ex = ToolExecutor(policy=_Policy(), review_gate=gate, review_blocked_hook=hook)
        spy = _Spy()
        ex.register_handler(spec.name, spy, spec=spec)

        result = await ex.execute(spec.name, {"table": "orders"})
        assert result.success
        assert called == []  # 闸门直通 ⇒ 钩子永不触发
        assert spy.calls == [{"table": "orders"}]

    @pytest.mark.asyncio
    async def test_hook_none_default_matches_p33_contract(self, registered_tool):
        """hook=None（缺省）时行为与 p33 钉下的契约逐位一致。"""
        spec = registered_tool("delete_records", risk_level="L2")
        gate = HumanReviewGate(enabled=True, approver=None)
        ex = ToolExecutor(policy=_Policy(), review_gate=gate)
        ex.register_handler(spec.name, _Spy(), spec=spec)

        result, request_id = await _block(ex, spec.name, {"table": "orders"})
        assert result.metadata["risk_level"] == RiskLevel.CRITICAL.value
        assert gate.get(request_id) is not None


# ---------------------------------------------------------------------------
# 2. 挂起流：拦下 → 桥 → Sidecar / 状态 / breakpoint
# ---------------------------------------------------------------------------


class TestSuspendFlow:
    @pytest.mark.asyncio
    async def test_block_opens_sidecar_and_counts(self, registered_tool):
        spec = registered_tool("delete_records", risk_level="L2")
        gate, sidecar, bridge = _make_stack()
        ex = _make_executor(gate, bridge)
        ex.register_handler(spec.name, _Spy(), spec=spec)

        result, request_id = await _block(ex, spec.name, {"table": "orders"})
        stats = bridge.stats()
        assert stats["created"] == 1
        assert stats["pending"] == 1
        assert len(sidecar.pending_ids) == 1
        pending = bridge.pending()
        assert pending[0]["tool_name"] == spec.name
        assert pending[0]["arguments"] == {"table": "orders"}  # 原始 args，非截断 digest
        assert gate.get(request_id) is not None  # 闸门侧登记仍在

    @pytest.mark.asyncio
    async def test_state_suspends_to_waiting_approval_and_resumes(self, registered_tool):
        spec = registered_tool("delete_records", risk_level="L2")
        gate, sidecar, bridge = _make_stack()

        state = AgentState(run_id="r1")
        LifecycleManager.transition(state, AgentStatus.RUNNING)
        gate_req = _blocked_request(gate, spec.name, {"table": "t"}, spec)

        suspend = await bridge.open_from_gate(gate_req, {"table": "t"}, state=state)
        assert state.status is AgentStatus.WAITING_APPROVAL

        decision = await bridge.decide(
            suspend.sidecar_request_id, ReviewDecisionAction.APPROVE, state=state
        )
        assert decision.approved
        assert state.status is AgentStatus.RUNNING

    @pytest.mark.asyncio
    async def test_breakpoint_recorded_and_resolved(self, registered_tool):
        spec = registered_tool("delete_records", risk_level="L2")
        fake = _FakeBreakpoints()
        gate, sidecar, bridge = _make_stack(breakpoints=fake)

        gate_req = _blocked_request(gate, spec.name, {"table": "t"}, spec)
        suspend = await bridge.open_from_gate(gate_req, {"table": "t"}, thread_id="th-1")
        assert len(fake.interrupts) == 1
        assert fake.interrupts[0].reason.value == "human_approval"
        assert fake.interrupts[0].thread_id == "th-1"
        assert fake.interrupts[0].state_snapshot["gate_request_id"] == gate_req.request_id

        await bridge.decide(suspend.sidecar_request_id, ReviewDecisionAction.APPROVE)
        assert fake.resolved and fake.resolved[0][1] == "continue"

    @pytest.mark.asyncio
    async def test_breakpoint_failure_is_best_effort(self, registered_tool):
        spec = registered_tool("delete_records", risk_level="L2")
        fake = _FakeBreakpoints(fail=True)
        gate, sidecar, bridge = _make_stack(breakpoints=fake)
        ex = _make_executor(gate, bridge)
        ex.register_handler(spec.name, _Spy(), spec=spec)

        result = await ex.execute(spec.name, {"table": "t"})
        assert result.metadata["reason"] == "review_required"  # 故障自吞，拦下照常
        assert bridge.stats()["pending"] == 1


# ---------------------------------------------------------------------------
# 3. 四裁决：APPROVE / REJECT / RESUME / EDIT
# ---------------------------------------------------------------------------


class TestDecideFlows:
    @pytest.mark.asyncio
    async def test_approve_then_retry_executes_once(self, registered_tool):
        spec = registered_tool("delete_records", risk_level="L2")
        gate, sidecar, bridge = _make_stack()
        spy = _Spy()
        ex = _make_executor(gate, bridge)
        ex.register_handler(spec.name, spy, spec=spec)

        await _block(ex, spec.name, {"table": "orders"})
        sid = bridge.pending()[0]["request_id"]
        decision = await bridge.decide(sid, ReviewDecisionAction.APPROVE)
        assert decision.approved and decision.result_action is ReviewDecisionAction.APPROVE
        assert bridge.stats()["approved"] == 1

        retry = await ex.execute(spec.name, {"table": "orders"})
        assert retry.success and len(spy.calls) == 1

        # 一次批准 = 一次执行：第二次再拦
        again = await ex.execute(spec.name, {"table": "orders"})
        assert not again.success
        assert again.metadata["reason"] == "review_required"

    @pytest.mark.asyncio
    async def test_reject_keeps_fail_closed(self, registered_tool):
        spec = registered_tool("delete_records", risk_level="L2")
        gate, sidecar, bridge = _make_stack()
        spy = _Spy()
        ex = _make_executor(gate, bridge)
        ex.register_handler(spec.name, spy, spec=spec)

        _, rid1 = await _block(ex, spec.name, {"table": "orders"})
        sid = bridge.pending()[0]["request_id"]
        decision = await bridge.decide(sid, ReviewDecisionAction.REJECT)
        assert not decision.approved
        assert gate.get(rid1).decision is ReviewDecision.REJECTED

        _, rid2 = await _block(ex, spec.name, {"table": "orders"})
        assert rid2 != rid1  # 新请求，旧拒绝不复用
        assert spy.calls == []
        stats = bridge.stats()
        assert stats["rejected"] == 1
        assert stats["rejection_rate"] == 1.0

    @pytest.mark.asyncio
    async def test_resume_is_approve(self, registered_tool):
        spec = registered_tool("delete_records", risk_level="L2")
        gate, sidecar, bridge = _make_stack()
        ex = _make_executor(gate, bridge)
        ex.register_handler(spec.name, _Spy(), spec=spec)

        await _block(ex, spec.name, {"table": "t"})
        sid = bridge.pending()[0]["request_id"]
        decision = await bridge.decide(sid, ReviewDecisionAction.RESUME)
        assert decision.approved
        retry = await ex.execute(spec.name, {"table": "t"})
        assert retry.success

    @pytest.mark.asyncio
    async def test_edit_merges_and_retry_passes(self, registered_tool):
        """EDIT：合并 edits → digest/risk 同式重算 → 执行点带 merged 重试放行。"""
        spec = registered_tool("delete_records", risk_level="L2")  # 名称 → CRITICAL：digest 必校验
        gate, sidecar, bridge = _make_stack()
        spy = _Spy()
        ex = _make_executor(gate, bridge)
        ex.register_handler(spec.name, spy, spec=spec)

        await _block(ex, spec.name, {"table": "orders"})
        sid = bridge.pending()[0]["request_id"]
        decision = await bridge.decide(
            sid, ReviewDecisionAction.EDIT, edited_arguments={"table": "payments"}
        )
        assert decision.approved
        assert decision.merged_arguments == {"table": "payments"}
        gr = gate.get(decision.gate_request_id)
        assert gr.args_digest == {"table": "payments"}
        assert gr.risk_level is RiskLevel.CRITICAL

        retry = await ex.execute(spec.name, {"table": "payments"})
        assert retry.success and spy.calls == [{"table": "payments"}]

    @pytest.mark.asyncio
    async def test_edit_escalation_updates_approval_scope(self, registered_tool):
        """编辑加 ``force`` ⇒ 审批记录升级 CRITICAL 并重算摘要（scope=所批即所跑）。"""
        spec = registered_tool("import_data", risk_level="L0")  # 名称 → MEDIUM
        gate, sidecar, bridge = _make_stack()
        ex = _make_executor(gate, bridge)
        ex.register_handler(spec.name, _Spy(), spec=spec)

        await _block(ex, spec.name, {"table": "orders"})
        sid = bridge.pending()[0]["request_id"]
        decision = await bridge.decide(
            sid, ReviewDecisionAction.EDIT, edited_arguments={"force": True}
        )
        assert decision.approved
        gr = gate.get(decision.gate_request_id)
        assert gr.risk_level is RiskLevel.CRITICAL  # 提权已进审批范围
        assert "force" in gr.args_digest

        merged = {"table": "orders", "force": True}
        retry = await ex.execute(spec.name, merged)
        assert retry.success  # preflight 重算同为 CRITICAL + digest 一致

    @pytest.mark.asyncio
    async def test_edit_with_approver_denied_fail_closed(self, registered_tool):
        spec = registered_tool("delete_records", risk_level="L2")
        gate = HumanReviewGate(enabled=True)
        deny_sidecar = ReviewerSidecar(approver=lambda pub: False)  # approver fail-closed
        bridge = HitlBridge(gate=gate, sidecar=deny_sidecar)
        ex = _make_executor(gate, bridge)
        ex.register_handler(spec.name, _Spy(), spec=spec)

        result = await ex.execute(spec.name, {"table": "t"})
        sid = bridge.pending()[0]["request_id"]
        decision = await bridge.decide(
            sid, ReviewDecisionAction.EDIT, edited_arguments={"table": "x"}
        )
        assert not decision.approved
        assert decision.result_action is ReviewDecisionAction.REJECT
        assert decision.reason == "approver_denied"
        assert gate.get(result.metadata["request_id"]).decision is ReviewDecision.REJECTED

    @pytest.mark.asyncio
    async def test_edit_without_edits_errors_and_keeps_pending(self, registered_tool):
        spec = registered_tool("delete_records", risk_level="L2")
        gate, sidecar, bridge = _make_stack()
        ex = _make_executor(gate, bridge)
        ex.register_handler(spec.name, _Spy(), spec=spec)

        result = await ex.execute(spec.name, {"table": "t"})
        rid = result.metadata["request_id"]
        sid = bridge.pending()[0]["request_id"]
        decision = await bridge.decide(sid, ReviewDecisionAction.EDIT, edited_arguments=None)
        assert decision.error and not decision.approved
        assert gate.get(rid).decision is ReviewDecision.PENDING  # 未落任何判定
        assert bridge.stats()["decided"] == 0

    @pytest.mark.asyncio
    async def test_unknown_decide_is_not_found(self):
        _, _, bridge = _make_stack()
        decision = await bridge.decide("nope", ReviewDecisionAction.APPROVE)
        assert isinstance(decision, BridgeDecision)
        assert decision.not_found and not decision.approved
        assert bridge.stats()["not_found"] == 1


# ---------------------------------------------------------------------------
# 4. 重启恢复 E2E
# ---------------------------------------------------------------------------


class TestRestartRecovery:
    @pytest.mark.asyncio
    async def test_roundtrip_restores_pending_and_gate_then_approve(self, registered_tool):
        spec = registered_tool("delete_records", risk_level="L2")
        gate, sidecar, bridge = _make_stack()
        ex = _make_executor(gate, bridge)
        ex.register_handler(spec.name, _Spy(), spec=spec)

        await _block(ex, spec.name, {"table": "orders"})
        payload = bridge.to_persistence()
        assert payload["mapping"] and payload["gate_requests"]

        # 重启：新闸门 + 新桥
        gate2 = HumanReviewGate(enabled=True)
        bridge2 = HitlBridge.from_persistence(payload, gate=gate2)
        assert bridge2.pending(), "Sidecar pending 必须跨重启恢复"
        sid = bridge2.pending()[0]["request_id"]

        decision = await bridge2.decide(sid, ReviewDecisionAction.APPROVE)
        assert decision.approved and not decision.gate_recovered
        # 恢复的闸门记录已批准 ⇒ preflight 放行并消费
        matched = gate2.preflight(spec.name, {"table": "orders"})
        assert matched is not None and matched.decision is ReviewDecision.APPROVED

    @pytest.mark.asyncio
    async def test_gate_record_lost_is_recovered_on_decide(self, registered_tool):
        """闸门记录丢失（容量淘汰/未恢复）⇒ decide 按 Sidecar 载荷重建。"""
        spec = registered_tool("delete_records", risk_level="L2")
        gate, sidecar, bridge = _make_stack()
        ex = _make_executor(gate, bridge)
        ex.register_handler(spec.name, _Spy(), spec=spec)

        await _block(ex, spec.name, {"table": "orders"})
        payload = bridge.to_persistence()
        payload["gate_requests"] = []  # 模拟闸门记录丢失

        gate2 = HumanReviewGate(enabled=True)
        bridge2 = HitlBridge.from_persistence(payload, gate=gate2)
        sid = bridge2.pending()[0]["request_id"]
        decision = await bridge2.decide(sid, ReviewDecisionAction.APPROVE)
        assert decision.approved and decision.gate_recovered
        assert gate2.preflight(spec.name, {"table": "orders"}) is not None

    @pytest.mark.asyncio
    async def test_persistence_carries_permission_mode(self):
        gate = HumanReviewGate(enabled=True)
        bridge = HitlBridge(gate=gate, permission_mode=PermissionMode.BYPASS)
        restored = HitlBridge.from_persistence(bridge.to_persistence(), gate=gate)
        assert restored.stats()["mode"] == "bypass"


# ---------------------------------------------------------------------------
# 5. permission_mode 三模式
# ---------------------------------------------------------------------------


class TestPermissionModes:
    @pytest.mark.asyncio
    async def test_bypass_passes_single_shot_without_suspend(self, registered_tool):
        spec = registered_tool("delete_records", risk_level="L2")
        gate, sidecar, bridge = _make_stack(mode=PermissionMode.BYPASS)
        spy = _Spy()
        ex = _make_executor(gate, bridge)
        ex.register_handler(spec.name, spy, spec=spec)

        result = await ex.execute(spec.name, {"table": "orders"})
        assert result.success and len(spy.calls) == 1  # 桥代批 → 再闸 → 同一次调用放行
        assert sidecar.pending_ids == []  # 未挂 Sidecar
        stats = bridge.stats()
        assert stats["bypass"] == 1 and stats["created"] == 1

    @pytest.mark.asyncio
    async def test_accept_edits_auto_approves_plain_edit_tool(self, registered_tool):
        spec = registered_tool("write_notes", risk_level="L0")  # 名称 → HIGH，无提权
        gate, sidecar, bridge = _make_stack(mode=PermissionMode.ACCEPT_EDITS)
        ex = _make_executor(gate, bridge)
        ex.register_handler(spec.name, _Spy(), spec=spec)

        result = await ex.execute(spec.name, {"text": "hi"})
        assert result.success
        assert sidecar.pending_ids == []
        assert bridge.stats()["auto"] == 1

    @pytest.mark.asyncio
    async def test_accept_edits_escalation_still_suspends(self, registered_tool):
        spec = registered_tool("write_notes", risk_level="L0")
        gate, sidecar, bridge = _make_stack(mode=PermissionMode.ACCEPT_EDITS)
        ex = _make_executor(gate, bridge)
        ex.register_handler(spec.name, _Spy(), spec=spec)

        result = await ex.execute(spec.name, {"text": "hi", "force": True})
        assert not result.success
        assert result.metadata["reason"] == "review_required"
        assert len(sidecar.pending_ids) == 1  # 参数提权 ⇒ 仍挂起

    @pytest.mark.asyncio
    async def test_accept_edits_declared_l2_still_suspends(self, registered_tool):
        """声明 L2（需人批）的编辑类工具：声明参与拦截 ⇒ 不自动放行。"""
        spec = registered_tool("edit_config", risk_level="L2")
        gate, sidecar, bridge = _make_stack(mode=PermissionMode.ACCEPT_EDITS)
        ex = _make_executor(gate, bridge)
        ex.register_handler(spec.name, _Spy(), spec=spec)

        result = await ex.execute(spec.name, {"key": "x"})
        assert not result.success
        assert result.metadata["reason"] == "review_required"
        assert len(sidecar.pending_ids) == 1

    def test_default_mode_is_manual(self):
        _, _, bridge = _make_stack()
        assert bridge.stats()["mode"] == PermissionMode.MANUAL.value


# ---------------------------------------------------------------------------
# 6. 观测 + 生产接线
# ---------------------------------------------------------------------------


class TestObservabilityAndWiring:
    @pytest.mark.asyncio
    async def test_stats_shape_and_mixed_rejection_rate(self, registered_tool):
        spec_a = registered_tool("delete_records", risk_level="L2")
        spec_b = registered_tool("import_data", risk_level="L0")
        gate, sidecar, bridge = _make_stack()
        ex = _make_executor(gate, bridge)
        ex.register_handler(spec_a.name, _Spy(), spec=spec_a)
        ex.register_handler(spec_b.name, _Spy(), spec=spec_b)

        await ex.execute(spec_a.name, {"table": "t"})
        await bridge.decide(bridge.pending()[0]["request_id"], ReviewDecisionAction.APPROVE)
        await ex.execute(spec_b.name, {"table": "t"})
        await bridge.decide(bridge.pending()[0]["request_id"], ReviewDecisionAction.REJECT)

        stats = bridge.stats()
        for key in (
            "created", "approved", "rejected", "edited", "resumed",
            "auto", "bypass", "not_found", "pending", "decided", "rejection_rate",
        ):
            assert key in stats
        assert stats["created"] == 2
        assert stats["decided"] == 2
        assert stats["rejection_rate"] == 0.5  # 拒批占比 = 误拒率的可观测分子分母
        assert stats["pending"] == 0

    @pytest.mark.asyncio
    async def test_hook_exception_does_not_change_result(self, registered_tool):
        spec = registered_tool("delete_records", risk_level="L2")

        async def broken_hook(request, args):
            raise RuntimeError("bridge exploded")

        gate = HumanReviewGate(enabled=True, approver=None)
        ex = ToolExecutor(policy=_Policy(), review_gate=gate, review_blocked_hook=broken_hook)
        ex.register_handler(spec.name, _Spy(), spec=spec)

        result = await ex.execute(spec.name, {"table": "t"})
        assert not result.success
        assert result.metadata["reason"] == "review_required"  # fail-closed 不受钩子影响

    def test_install_executor_hook_is_idempotent_and_uninstallable(self):
        reset_hitl_bridge()
        reset_tool_executor()
        try:
            bridge = install_executor_hook()
            assert get_tool_executor()._review_blocked_hook is not None
            assert install_executor_hook() is bridge  # 幂等：同一单例
            get_tool_executor().set_review_blocked_hook(None)  # 卸载回零变化路径
            assert get_tool_executor()._review_blocked_hook is None
        finally:
            reset_tool_executor()
            reset_hitl_bridge()

    def test_singleton_returns_same_bridge(self):
        reset_hitl_bridge()
        try:
            assert get_hitl_bridge() is get_hitl_bridge()
        finally:
            reset_hitl_bridge()

    @pytest.mark.asyncio
    async def test_default_gate_follows_settings_flag_off(self):
        """缺省桥（未注入）吃全局闸门：settings 关 ⇒ stats.enabled=False。"""
        reset_hitl_bridge()
        try:
            assert get_hitl_bridge().stats()["enabled"] is False
        finally:
            reset_hitl_bridge()
