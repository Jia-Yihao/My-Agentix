"""P4（岗位对照·异常闭环 4.3）：计划态权限切换 + RollbackPort 生产实现。

借鉴 claw-code ``tools/src/lib.rs:6013-6081``「计划态 = 可回滚的**配置级**
权限切换（保存/恢复）」（source-code-study §5 4.3）。钉死契约：

1. **flag 治理**：``AGENT_ROLLBACK_ENABLED`` 默认 False ⇒ 声明清单不含
   计划态工具、``install_plan_mode_tools()`` 零接线（与基线逐项一致）；
2. **声明契约**：L0 + ``side_effect=True`` + ``capabilities=("control",)`` +
   ``timeout_ms=5000``；schema 类名 == 工具名（title 契约——否则
   ``selection._spec_for_schema`` 回查静默 miss）；名称不命中 HITL 词表
   ⇒ 有效风险 LOW ⇒ 免审批；
3. **存储**：``InMemoryRollbackStore`` 实现 ``RollbackPort``（isinstance
   可判）、deepcopy 双向隔离、restore 缺失抛 ``KeyError``（不静默降级）、
   per-run 检查点 LRU 有界 + run 数有界；
4. **生命周期**：enter 快照原策略 → 收紧 L0；**幂等红线**（二次 enter 不
   换 checkpoint，否则原检查点被 L0 覆盖、退出形同没退）；exit 按
   checkpoint 恢复（``None`` 也是合法恢复值）；未 enter / 未知 checkpoint
   诚实失败；``reset_rollback_store`` 清标记**不动 executor.policy**
   （残留 L0 是可见状态，不藏副作用）；
5. **端到端**：flag-on 安装后经 ``ToolExecutor.execute`` 进出计划态，
   L1 工具在计划态内被风险闸门拒绝、退出后恢复可执行。
"""
from __future__ import annotations

import pytest

from agentix.application.services.rollback import (
    MAX_CHECKPOINTS_PER_RUN,
    PLAN_RUN_ID,
    InMemoryRollbackStore,
    enter_plan_mode,
    exit_plan_mode,
    install_plan_mode_tools,
    reset_rollback_store,
)
from agentix.tools import registry_init
from agentix.tools.definitions import ToolSpec, registry as tool_registry
from agentix.tools.gateway.executor import get_tool_executor, reset_tool_executor
from agentix.tools.adapters.plan_mode import (
    MAX_REASON_CHARS,
    build_enter_plan_mode_spec,
    build_exit_plan_mode_spec,
    enter_plan_mode as EnterSchema,
    exit_plan_mode as ExitSchema,
)
from agentix.tools.selection import ToolSelectionPolicy
from agentix.config import settings
from agentix.domain.ports import RollbackPort
from agentix.tools.gateway.human_review import RiskLevel, effective_risk_level

# ---------------------------------------------------------------------------
# 基线断言：flag 默认关（flag 治理；见 配置管理.md §5）
# ---------------------------------------------------------------------------

assert settings.AGENT_ROLLBACK_ENABLED is False, "P4 基线：计划态默认关"

_TEST_TOOLS = ("enter_plan_mode", "exit_plan_mode", "t_p55_l1")


def _purge() -> None:
    registry_init._DECLARATIONS.clear()
    reset_rollback_store()
    reset_tool_executor()
    for name in _TEST_TOOLS:
        if tool_registry.has_tool(name):
            tool_registry.delete_tool(name)


@pytest.fixture(autouse=True)
def _clean():
    _purge()
    yield
    _purge()


def _ok_handler(args):
    return {"ok": True}


# ---------------------------------------------------------------------------
# 1. flag 治理（声明清单 / 安装接线）
# ---------------------------------------------------------------------------


class TestFlagGovernance:
    def test_flag_off_declarations_exclude_plan_tools(self):
        registry_init._DECLARATIONS.clear()
        names = [spec.name for spec, _ in registry_init.get_declarations()]
        assert "enter_plan_mode" not in names
        assert "exit_plan_mode" not in names

    def test_flag_off_install_registers_nothing(self):
        install_plan_mode_tools()  # flag 关：内部首行返回
        ex = get_tool_executor()
        assert not ex.has_handler("enter_plan_mode")
        assert not ex.has_handler("exit_plan_mode")
        assert not tool_registry.has_tool("enter_plan_mode")

    def test_flag_on_declarations_and_title_contract(self, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_ROLLBACK_ENABLED", True)
        registry_init._DECLARATIONS.clear()
        decls = {spec.name: cls for spec, cls in registry_init.get_declarations()}
        assert "enter_plan_mode" in decls and "exit_plan_mode" in decls
        # title 契约：schema 类名 == 工具名（LLM 声明回查依赖）
        assert decls["enter_plan_mode"].__name__ == "enter_plan_mode"
        assert decls["exit_plan_mode"].__name__ == "exit_plan_mode"

    def test_flag_on_install_registers_handlers(self, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_ROLLBACK_ENABLED", True)
        registry_init._DECLARATIONS.clear()
        install_plan_mode_tools()
        ex = get_tool_executor()
        assert ex.has_handler("enter_plan_mode")
        assert ex.has_handler("exit_plan_mode")
        assert tool_registry.has_tool("enter_plan_mode")
        assert tool_registry.has_tool("exit_plan_mode")
        # 幂等：重复安装不炸
        install_plan_mode_tools()


# ---------------------------------------------------------------------------
# 2. 声明契约（L0 / side_effect / HITL 词表 / schema 形状）
# ---------------------------------------------------------------------------


class TestSpecContract:
    def test_specs_are_convergent_l0_contracts(self):
        for build in (build_enter_plan_mode_spec, build_exit_plan_mode_spec):
            spec = build()
            assert spec.risk_level == "L0", "计划态只收紧权限 ⇒ 声明 L0"
            assert spec.side_effect is True, "运行期策略切换 = 配置级副作用"
            assert tuple(spec.capabilities) == ("control",)
            assert spec.timeout_ms == 5000

    def test_names_evade_hitl_vocab(self):
        """名称不命中 HITL 词表 ⇒ 有效风险 LOW ⇒ 计划态免审批。"""
        assert (
            effective_risk_level("enter_plan_mode", {}, declared_risk_level="L0")
            is RiskLevel.LOW
        )
        assert (
            effective_risk_level("exit_plan_mode", {}, declared_risk_level="L0")
            is RiskLevel.LOW
        )

    def test_schema_shape(self):
        assert EnterSchema.__name__ == "enter_plan_mode"
        assert ExitSchema.__name__ == "exit_plan_mode"
        assert "reason" in EnterSchema.model_json_schema()["properties"]
        assert "checkpoint_id" in ExitSchema.model_json_schema()["properties"]
        assert MAX_REASON_CHARS == 400


# ---------------------------------------------------------------------------
# 3. 存储契约（RollbackPort / deepcopy 隔离 / 诚实失败 / LRU 有界）
# ---------------------------------------------------------------------------


class TestStoreContract:
    def test_is_rollback_port(self):
        assert isinstance(InMemoryRollbackStore(), RollbackPort)

    @pytest.mark.asyncio
    async def test_roundtrip_and_double_deepcopy_isolation(self):
        store = InMemoryRollbackStore()
        state = {"policy": {"max": "L1"}}
        cid = await store.snapshot(PLAN_RUN_ID, state)
        assert isinstance(cid, str) and cid
        state["policy"]["max"] = "L3"  # 快照隔离：改源不污染
        got = await store.restore(PLAN_RUN_ID, cid)
        assert got == {"policy": {"max": "L1"}}
        got["policy"]["max"] = "L0"  # 返回隔离：改返回值不污染存储
        got2 = await store.restore(PLAN_RUN_ID, cid)
        assert got2 == {"policy": {"max": "L1"}}

    @pytest.mark.asyncio
    async def test_restore_missing_raises_keyerror(self):
        store = InMemoryRollbackStore()
        cid = await store.snapshot("run-a", {"v": 1})
        with pytest.raises(KeyError):
            await store.restore("run-a", "deadbeef")
        with pytest.raises(KeyError):
            await store.restore("run-b", cid)

    @pytest.mark.asyncio
    async def test_checkpoint_lru_drops_oldest_on_touch(self):
        store = InMemoryRollbackStore(max_checkpoints_per_run=2)
        a = await store.snapshot("r", {"v": 1})
        b = await store.snapshot("r", {"v": 2})
        await store.restore("r", a)  # 命中即 touch ⇒ b 变最旧
        c = await store.snapshot("r", {"v": 3})
        with pytest.raises(KeyError):
            await store.restore("r", b)
        assert (await store.restore("r", a))["v"] == 1
        assert (await store.restore("r", c))["v"] == 3

    @pytest.mark.asyncio
    async def test_runs_are_bounded(self):
        store = InMemoryRollbackStore(max_runs=1)
        await store.snapshot("run1", {"v": 1})
        cid2 = await store.snapshot("run2", {"v": 2})
        with pytest.raises(KeyError):
            await store.restore("run1", "anything")
        assert (await store.restore("run2", cid2))["v"] == 2

    @pytest.mark.asyncio
    async def test_default_checkpoint_capacity_constant(self):
        assert MAX_CHECKPOINTS_PER_RUN == 8


# ---------------------------------------------------------------------------
# 4. 生命周期（enter 收紧 / 幂等红线 / exit 恢复 / 诚实失败）
# ---------------------------------------------------------------------------


class TestPlanModeLifecycle:
    @pytest.mark.asyncio
    async def test_enter_tightens_exit_restores_and_gates_l1(self):
        ex = get_tool_executor()
        ex.policy = ToolSelectionPolicy(max_risk_level="L1")
        if tool_registry.has_tool("t_p55_l1"):
            tool_registry.delete_tool("t_p55_l1")
        ex.register_handler(
            "t_p55_l1",
            _ok_handler,
            spec=ToolSpec(name="t_p55_l1", description="L1 tool", risk_level="L1"),
        )
        # 进入前：L1 工具可执行
        before = await ex.execute("t_p55_l1", {})
        assert before.success is True
        # enter：快照原策略 → 收紧 L0
        r_in = await enter_plan_mode(reason="探查")
        assert r_in["in_plan_mode"] and r_in["changed"] and not r_in["already_active"]
        assert r_in["previous_max_risk_level"] == "L1"
        assert ex.policy.max_risk_level == "L0"
        denied = await ex.execute("t_p55_l1", {})
        assert denied.success is False
        assert denied.metadata["reason"] == "risk_denied", "计划态内 L1 被闸"
        # 幂等红线：二次 enter 不换 checkpoint（否则退出形同没退）
        r_in2 = await enter_plan_mode()
        assert r_in2["already_active"] is True and r_in2["changed"] is False
        assert r_in2["checkpoint_id"] == r_in["checkpoint_id"]
        assert ex.policy.max_risk_level == "L0"
        # exit：恢复进入前的原策略
        r_out = await exit_plan_mode()
        assert r_out["in_plan_mode"] is False and r_out["changed"] is True
        assert r_out["max_risk_level"] == "L1"
        assert ex.policy.max_risk_level == "L1"
        after = await ex.execute("t_p55_l1", {})
        assert after.success is True, "退出后 L1 恢复可执行"

    @pytest.mark.asyncio
    async def test_enter_from_none_policy_restores_none(self):
        ex = get_tool_executor()
        assert ex.policy is None, "生产默认无策略约束"
        r_in = await enter_plan_mode()
        assert r_in["previous_max_risk_level"] is None
        assert ex.policy.max_risk_level == "L0"
        r_out = await exit_plan_mode()
        assert ex.policy is None, "恢复值就是快照值（None 合法）"
        assert r_out["max_risk_level"] is None

    @pytest.mark.asyncio
    async def test_exit_without_enter_raises(self):
        with pytest.raises(ValueError, match="plan mode is not active"):
            await exit_plan_mode()

    @pytest.mark.asyncio
    async def test_exit_unknown_checkpoint_fails_but_state_kept(self):
        ex = get_tool_executor()
        await enter_plan_mode()
        with pytest.raises(KeyError):
            await exit_plan_mode(checkpoint_id="deadbeef")
        assert ex.policy.max_risk_level == "L0", "失败不得静默降级状态"
        # 收尾：用正确 checkpoint 退出（fixture 也会兜底复位）
        await exit_plan_mode()

    @pytest.mark.asyncio
    async def test_reset_store_does_not_touch_executor_policy(self):
        ex = get_tool_executor()
        await enter_plan_mode()
        reset_rollback_store()
        # 残留 L0 是**可见状态**（不藏副作用）；计划态标记已清 ⇒ 退出诚实失败
        assert ex.policy.max_risk_level == "L0"
        with pytest.raises(ValueError):
            await exit_plan_mode()
        ex.policy = None  # 调用方显式清理

    @pytest.mark.asyncio
    async def test_reenter_after_exit_uses_fresh_checkpoint(self):
        r1 = await enter_plan_mode()
        await exit_plan_mode()
        r2 = await enter_plan_mode()
        assert r2["checkpoint_id"] != r1["checkpoint_id"], "退出后可再次进入"
        assert r2["changed"] is True and r2["already_active"] is False
        await exit_plan_mode()


# ---------------------------------------------------------------------------
# 5. 端到端：安装 → 经 ToolExecutor 执行两工具
# ---------------------------------------------------------------------------


class TestInstallAndExecuteE2E:
    @pytest.mark.asyncio
    async def test_execute_plan_tools_through_executor(self, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_ROLLBACK_ENABLED", True)
        registry_init._DECLARATIONS.clear()
        install_plan_mode_tools()
        ex = get_tool_executor()
        r_in = await ex.execute("enter_plan_mode", {"reason": "p55-e2e"})
        assert r_in.success is True
        assert r_in.data["in_plan_mode"] is True and r_in.data["changed"] is True
        assert ex.policy.max_risk_level == "L0"
        # 计划态内 exit 仍可执行（exit 声明 L0 ≤ L0 ⇒ 过风险闸门）
        r_out = await ex.execute("exit_plan_mode", {})
        assert r_out.success is True
        assert r_out.data["in_plan_mode"] is False
        assert ex.policy is None
