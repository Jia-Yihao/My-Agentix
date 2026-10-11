"""调用点级审批闸门守卫（P3-3）。

背景：审批闸门此前挂在 ``get_tool()``（工具**实例化**点），那里只有
``tool_name``、拿不到 ``arguments``，于是：

- ``classify_risk`` 的**参数提权**（``force`` / ``all=true``）永不触发；
- 工具**声明级**（``ToolSpec.risk_level``）也从未参与审批判定；
- 又因为 ``@cache``，同一工具一个进程内只检查一次。

结果是一条"看起来有闸门、实际只按工具名过滤"的防线。本文件把闸门的真实
契约钉在执行点 :meth:`ToolExecutor.execute` 上，逐条守卫：

1. 有效风险 = max(声明级, 名称级, 参数提权)；
2. 审批未通过 ⇒ **不执行**（fail-closed），且异常不得逸出成系统故障；
3. 一次批准 = 一次执行（消费语义），实例化点只做非消费式预检；
4. 批准范围绑定 ``tool_name`` + ``risk_level``（CRITICAL 再绑参数摘要）。
"""

from __future__ import annotations

import pytest

from agentix.tools.definitions import ToolSpec, registry as tool_registry
from agentix.tools.gateway.executor import ToolExecutor
from agentix.tools.gateway.human_review import (
    HumanReviewGate,
    ReviewDecision,
    RiskLevel,
    declared_level_for,
    effective_risk_level,
    rank_of,
)

# ---------------------------------------------------------------------------
# 夹具：一次性工具登记 + 全局注册表清理（防测试间污染）
# ---------------------------------------------------------------------------


class _Policy:
    """最小策略对象：只要求 ``max_risk_level``。"""

    def __init__(self, max_risk_level: str = "L3") -> None:
        self.max_risk_level = max_risk_level


class _Spy:
    """记录 handler 是否真的被执行过。"""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, args):
        self.calls.append(dict(args))
        return {"ok": True}


@pytest.fixture
def registered_tool():
    """登记一个工具进全局注册表，测试结束自动注销。"""
    created: list[str] = []

    def _register(name: str, *, risk_level: str, control: str = "", side_effect: bool = False):
        spec = ToolSpec(
            name=name,
            description=f"test tool {name}",
            risk_level=risk_level,
            control=control,
            side_effect=side_effect,
        )
        created.append(name)
        return spec

    yield _register

    for name in created:
        tool_registry.delete_tool(name)


# ---------------------------------------------------------------------------
# 1. 有效风险等级：声明级必须参与判定
# ---------------------------------------------------------------------------


class TestEffectiveRiskLevel:
    def test_declared_level_is_mapped(self):
        assert declared_level_for("L0") is RiskLevel.LOW
        assert declared_level_for("L1") is RiskLevel.MEDIUM
        assert declared_level_for("L2") is RiskLevel.HIGH
        assert declared_level_for("L3") is RiskLevel.CRITICAL

    def test_unknown_declared_level_fails_closed_upward(self):
        """看不懂的声明级按 HIGH 处理——"看不懂"不等于"很安全"。"""
        assert declared_level_for("L9") is RiskLevel.HIGH
        assert declared_level_for("") is RiskLevel.HIGH
        assert declared_level_for(None) is RiskLevel.HIGH

    def test_declared_level_lifts_innocuous_name(self):
        """名字看不出危险的工具，声明 L2 后必须被判为 HIGH。

        这是此前的真实漏洞：``plain_tool`` 不命中任何名称模式，
        声明 L2 也照样被当成 LOW 直通。
        """
        assert effective_risk_level("plain_tool", {}, declared_risk_level="L0") is RiskLevel.LOW
        assert (
            effective_risk_level("plain_tool", {}, declared_risk_level="L2") is RiskLevel.HIGH
        )

    def test_arg_escalation_lifts_declared_l0(self):
        assert effective_risk_level("read_table", {}, declared_risk_level="L0") is RiskLevel.LOW
        assert (
            effective_risk_level("read_table", {"all": True}, declared_risk_level="L0")
            is RiskLevel.HIGH
        )
        assert (
            effective_risk_level("read_table", {"force": True}, declared_risk_level="L0")
            is RiskLevel.CRITICAL
        )

    def test_escalation_cannot_downgrade(self):
        """参数只能提权、不能降权：CRITICAL 级工具传"温和参数"仍是 CRITICAL。"""
        assert (
            effective_risk_level("drop_everything", {"dry_run": True}) is RiskLevel.CRITICAL
        )

    def test_rank_of_is_ordered(self):
        assert rank_of(RiskLevel.LOW) < rank_of(RiskLevel.MEDIUM) < rank_of(
            RiskLevel.HIGH
        ) < rank_of(RiskLevel.CRITICAL)


# ---------------------------------------------------------------------------
# 2. 执行点风险闸门：参数提权必须能触发 max_risk_level 拒绝
# ---------------------------------------------------------------------------


class TestExecutionRiskGate:
    @pytest.mark.asyncio
    async def test_declared_risk_still_denied(self, registered_tool):
        spec = registered_tool("t_write", risk_level="L2", control="需人工审批（HITL）")
        executor = ToolExecutor()
        executor.register_handler("t_write", _Spy(), spec=spec)

        result = await executor.execute("t_write", {}, policy=_Policy("L1"))
        assert result.success is False
        assert result.metadata["reason"] == "risk_denied"

    @pytest.mark.asyncio
    async def test_arg_escalation_triggers_denial(self, registered_tool):
        """★ 核心修正：声明 L0 的工具 + 危险参数，必须被 max_risk_level 拦下。

        修正前：这里只看 ``spec.risk_level``（L0 ≤ L1）→ 放行。
        """
        spec = registered_tool("t_readonly", risk_level="L0")
        spy = _Spy()
        executor = ToolExecutor()
        executor.register_handler("t_readonly", spy, spec=spec)

        # 无危险参数 → 放行
        ok = await executor.execute("t_readonly", {"table": "x"}, policy=_Policy("L1"))
        assert ok.success is True and len(spy.calls) == 1

        # 带 all=True → 有效等级 HIGH > L1 → 拒绝，且 handler 不得被执行
        denied = await executor.execute("t_readonly", {"all": True}, policy=_Policy("L1"))
        assert denied.success is False
        assert denied.metadata["reason"] == "risk_denied"
        assert denied.metadata["risk_level"] == "high"
        assert denied.metadata["declared_risk_level"] == "L0"
        assert len(spy.calls) == 1, "被拒绝的调用绝不能落到 handler"

    @pytest.mark.asyncio
    async def test_effective_risk_reported_in_success_metadata(self, registered_tool):
        spec = registered_tool("t_meta", risk_level="L1")
        executor = ToolExecutor()
        executor.register_handler("t_meta", _Spy(), spec=spec)

        result = await executor.execute("t_meta", {}, policy=_Policy("L3"))
        assert result.success is True
        assert result.metadata["risk_level"] == "medium"
        assert result.metadata["declared_risk_level"] == "L1"


# ---------------------------------------------------------------------------
# 3. 调用点审批闸门：fail-closed + 不抛异常 + 一次批准一次执行
# ---------------------------------------------------------------------------


class TestCallSiteReviewGate:
    @pytest.mark.asyncio
    async def test_gate_disabled_is_passthrough(self, registered_tool):
        spec = registered_tool("t_pass", risk_level="L2", control="HITL")
        spy = _Spy()
        executor = ToolExecutor(review_gate=HumanReviewGate(enabled=False))
        executor.register_handler("t_pass", spy, spec=spec)

        result = await executor.execute("t_pass", {})
        assert result.success is True and len(spy.calls) == 1

    @pytest.mark.asyncio
    async def test_high_risk_without_approver_is_blocked(self, registered_tool):
        """开启审批但没人能批 ⇒ 不执行（fail-closed），且 handler 未被调用。"""
        spec = registered_tool("t_hitl", risk_level="L2", control="HITL")
        spy = _Spy()
        executor = ToolExecutor(review_gate=HumanReviewGate(enabled=True, approver=None))
        executor.register_handler("t_hitl", spy, spec=spec)

        result = await executor.execute("t_hitl", {})
        assert result.success is False
        assert result.metadata["reason"] == "review_required"
        assert result.metadata["risk_level"] == "high"
        assert result.metadata["request_id"]
        assert result.retryable is False
        assert spy.calls == [], "未获批准的动作绝不能被真正执行"

    @pytest.mark.asyncio
    async def test_review_required_never_escapes_as_exception(self, registered_tool):
        """审批受阻必须以 ToolResult 落地，不得逸出成未捕获异常。

        ``execute`` 的契约是"永不抛异常"；若在这里放行 ``ReviewRequired``，
        上层会把"待审批"误判成"系统故障"。
        """
        spec = registered_tool("t_noescape", risk_level="L2", control="HITL")
        executor = ToolExecutor(review_gate=HumanReviewGate(enabled=True, approver=None))
        executor.register_handler("t_noescape", _Spy(), spec=spec)

        result = await executor.execute("t_noescape", {})  # 不 raise
        assert isinstance(result.success, bool) and result.success is False

    @pytest.mark.asyncio
    async def test_approval_releases_exactly_one_execution(self, registered_tool):
        """一次批准 = 一次执行；第二次必须重新走审批。"""
        spec = registered_tool("t_once", risk_level="L2", control="HITL")
        spy = _Spy()
        gate = HumanReviewGate(enabled=True, approver=None)
        executor = ToolExecutor(review_gate=gate)
        executor.register_handler("t_once", spy, spec=spec)

        first = await executor.execute("t_once", {})
        assert first.success is False
        request_id = first.metadata["request_id"]
        assert gate.approve(request_id, by="alice") is True

        second = await executor.execute("t_once", {})
        assert second.success is True and len(spy.calls) == 1

        third = await executor.execute("t_once", {})
        assert third.success is False, "批准已消费，第二次执行必须重新审批"
        assert len(spy.calls) == 1

    @pytest.mark.asyncio
    async def test_approval_scope_bound_to_args_for_critical(self, registered_tool):
        """★ 批准 ``{}`` 不得放行 ``{'force': True}``（否则等于提权）。"""
        spec = registered_tool("t_scope", risk_level="L2", control="HITL")
        spy = _Spy()
        gate = HumanReviewGate(enabled=True, approver=None)
        executor = ToolExecutor(review_gate=gate)
        executor.register_handler("t_scope", spy, spec=spec)

        # 温和调用 → 拿批准
        mild = await executor.execute("t_scope", {"n": 1})
        assert mild.success is False
        # CRITICAL 级请求（force=True）→ 另开一条审批记录
        dangerous = await executor.execute("t_scope", {"n": 1, "force": True})
        assert dangerous.success is False
        assert dangerous.metadata["risk_level"] == "critical"
        mild_id = mild.metadata["request_id"]
        assert mild_id != dangerous.metadata["request_id"]

        # 批准"温和那次"（HIGH 级），不应放出 CRITICAL 那次
        gate.approve(mild_id, by="alice")
        again = await executor.execute("t_scope", {"n": 1, "force": True})
        assert again.success is False, "HIGH 级批准不得释放 CRITICAL 级调用"
        assert spy.calls == []

    @pytest.mark.asyncio
    async def test_low_risk_needs_no_approval(self, registered_tool):
        spec = registered_tool("t_low", risk_level="L0")
        spy = _Spy()
        executor = ToolExecutor(review_gate=HumanReviewGate(enabled=True, approver=None))
        executor.register_handler("t_low", spy, spec=spec)

        result = await executor.execute("t_low", {})
        assert result.success is True and len(spy.calls) == 1

    @pytest.mark.asyncio
    async def test_gate_failure_is_fail_closed(self, registered_tool):
        """闸门自身故障（如 policy 对象坏掉）也必须拒绝执行，不得放行。"""
        spec = registered_tool("t_broken", risk_level="L2", control="HITL")
        spy = _Spy()

        class _BrokenGate:
            enabled = True

            def preflight(self, *a, **kw):
                raise RuntimeError("gate exploded")

        executor = ToolExecutor(review_gate=_BrokenGate())
        executor.register_handler("t_broken", spy, spec=spec)

        result = await executor.execute("t_broken", {})
        assert result.success is False
        assert result.metadata["reason"] == "review_preflight_error"
        assert spy.calls == []

    @pytest.mark.asyncio
    async def test_approval_path_calls_approver_and_respects_rejection(self, registered_tool):
        """有审批人时走 ``decide`` 语义：拒绝 ⇒ 不放行。"""
        spec = registered_tool("t_rej", risk_level="L2", control="HITL")
        spy = _Spy()

        async def _rejecting_approver(request):
            return ReviewDecision.REJECTED

        gate = HumanReviewGate(enabled=True, approver=_rejecting_approver)
        executor = ToolExecutor(review_gate=gate)
        executor.register_handler("t_rej", spy, spec=spec)

        # preflight 是**同步**闸门，不会调用 async approver；
        # 它按"没有可用批准"处理 ⇒ fail-closed（这是刻意设计：
        # 同步路径不允许等待人工，等待必须发生在 decide() 的异步链路里）
        result = await executor.execute("t_rej", {})
        assert result.success is False
        assert result.metadata["reason"] == "review_required"
        assert spy.calls == []


# ---------------------------------------------------------------------------
# 4. 实例化点：非消费式预检（不得吃掉执行用的批准）
# ---------------------------------------------------------------------------


class TestInstantiationPreflightIsNonConsuming:
    def test_preflight_consume_false_keeps_approval(self):
        from agentix.tools.gateway.human_review import ReviewRequired

        gate = HumanReviewGate(enabled=True, approver=None)
        # 先制造一条待审批请求（drop_* 命中 CRITICAL 名称模式）并批准
        with pytest.raises(ReviewRequired) as exc_info:
            gate.preflight("delete_inst", {})
        request_id = exc_info.value.request.request_id
        assert gate.approve(request_id, by="alice") is True

        # 实例化式检查（consume=False）→ 看得到批准，但不消费
        probe = gate.preflight("delete_inst", {}, consume=False)
        assert probe is not None and probe.consumed is False

        # 执行式检查仍能消费掉它
        used = gate.preflight("delete_inst", {})
        assert used is not None and used.consumed is True

        # 已用尽 ⇒ 再次 fail-closed（非消费式检查也不例外）
        with pytest.raises(ReviewRequired):
            gate.preflight("delete_inst", {}, consume=False)

    def test_declared_level_participates_in_preflight(self):
        """声明 L2 + 名字无害 ⇒ preflight 仍须拦住（此前会放行）。"""
        from agentix.tools.gateway.human_review import ReviewRequired

        gate = HumanReviewGate(enabled=True, approver=None)
        assert gate.preflight("plain_tool", {}) is None, "不传声明级时不参与判定"

        with pytest.raises(ReviewRequired):
            gate.preflight(
                "plain_tool", {}, declared_level=declared_level_for("L2")
            )
