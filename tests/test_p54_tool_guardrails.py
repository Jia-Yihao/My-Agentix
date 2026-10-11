"""P4（岗位对照·异常闭环 4.1 + 4.2）：工具失败三计数器护栏 + 重复结果折叠。

借鉴 hermes ``tool_guardrails.py`` 与结果出层折叠（source-code-study §5 4.1/4.2）：
源侧在工具失败后有**三计数器分级**与**工具专属恢复建议回喂**，重复字节级
结果被折叠成 stub——本仓此前只有「失败转数据」，没有「失败后系统性变聪明」。

钉死契约（DoD）：

1. **flag 治理**：``TOOL_GUARDRAILS_ENABLED`` 默认 False ⇒ executor /
   task_retry / 两处 dedup 接线**零调用**，失败 error 文本与基线逐位等价；
2. **三计数器判级**：同参连续失败 2/3/5 → warn/block/halt；工具级失败
   2/5/10 → warn/block/halt；只读成功打断「同参连续」链但保留 tool_failures、
   mutation 成功**全清**；block 只拦同参、halt 拦全部；
3. **error 文本不被建议污染**（``is_retryable`` / ``failure_key`` 指纹稳定），
   建议只进 ``metadata["guardrail"]`` / ``TaskOutcome.error`` 尾部；
4. **建议文本不含 is_retryable 的 ASCII 提示词**（双保险——拼接进 error
   后不得改变可重试性分类）；
5. **task_retry**：flag-on 时 block/halt ⇒ ``skipped_reason="guardrail_*"``
   且 runner 一次都不启动；重试耗尽的终局 error 追加「恢复建议：」；
6. **RetryReplanner**：注入 guardrails ⇒ rationale 拼建议；未注入/无记录/
   引擎故障 ⇒ 逐位等价原样；
7. **重复结果 stub**（W2）：第 2 次字节级重复起折叠（轮询不豁免），
   ``MappingStepExecutor`` 与 ``_to_observation`` 两接点均 flag 门控。
"""
from __future__ import annotations

import pytest

from agentix.application.services.task_plan import TaskSpec as PipelineTaskSpec
from agentix.application.services.task_retry import TaskOutcome, run_task
from agentix.orchestration.executor import MappingStepExecutor
from agentix.policies.planner.retry import RetryReplanner
from agentix.tools import registry_init
from agentix.tools.definitions import ToolSpec, registry as tool_registry
from agentix.tools.gateway.executor import ToolExecutor, reset_tool_executor
from agentix.policies.guardrails.guardrails import (
    GuardrailTier,
    ToolGuardrails,
    get_tool_guardrails,
    recovery_advice,
    reset_tool_guardrails,
)
from agentix.tools.gateway.result_limiter import (
    ResultDeduplicator,
    get_result_deduplicator,
    reset_result_deduplicator,
)
from agentix.config import settings
from agentix.domain.agent.types import ContextSnapshot, Plan, PlanStep, TaskSpec
from agentix.domain.policies.error_classification import (
    NON_RETRYABLE_HINTS,
    RETRYABLE_HINTS,
)

# ---------------------------------------------------------------------------
# 基线断言：三开关默认关（flag 治理；见 configuration.md §5）
# ---------------------------------------------------------------------------

assert settings.TOOL_GUARDRAILS_ENABLED is False, "P4 基线：护栏默认关"

# is_retryable 的 ASCII 提示词（domain/policies/error_classification.py）——
# 恢复建议不得携带其中任何一个，否则拼进 error 后会污染可重试性分类。
_HINTS = tuple(h.lower() for h in NON_RETRYABLE_HINTS + RETRYABLE_HINTS)


@pytest.fixture(autouse=True)
def _clean():
    """隔离：护栏引擎 / 结果折叠引擎 / 执行器单例 / 注册表逐测清零。"""
    reset_tool_guardrails()
    reset_result_deduplicator()
    reset_tool_executor()
    yield
    reset_tool_guardrails()
    reset_result_deduplicator()
    reset_tool_executor()
    for name in ("t_p54_fail", "t_p54_ok", "t_p54_poll"):
        if tool_registry.has_tool(name):
            tool_registry.delete_tool(name)


class _ToggleTool:
    """可切换成败的 handler（失败时同步抛 ValueError）。"""

    def __init__(self, *, fail: bool = True) -> None:
        self.fail = fail
        self.calls = 0

    def __call__(self, args):
        self.calls += 1
        if self.fail:
            raise ValueError("boom")
        return {"ok": True}


def _make_executor(name: str, handler, *, side_effect: bool = False) -> ToolExecutor:
    if tool_registry.has_tool(name):
        tool_registry.delete_tool(name)
    ex = ToolExecutor()
    ex.register_handler(
        name,
        handler,
        spec=ToolSpec(
            name=name,
            description=f"test tool {name}",
            risk_level="L0",
            side_effect=side_effect,
        ),
    )
    return ex


# ---------------------------------------------------------------------------
# 1. 引擎判级（flag 无关——引擎实例自身不读 settings）
# ---------------------------------------------------------------------------


class TestGuardrailEngine:
    def test_same_args_escalation_2_3_5(self):
        g = ToolGuardrails()
        args = {"q": "x"}
        v1 = g.record_failure("t", args, error="boom")
        assert v1.tier is GuardrailTier.OK and v1.advice == "" and not v1.rejected
        v2 = g.record_failure("t", args, error="boom")
        assert v2.tier is GuardrailTier.WARN and v2.advice and not v2.rejected
        assert v2.same_args_failures == 2
        v3 = g.record_failure("t", args, error="boom")
        assert v3.tier is GuardrailTier.BLOCK and v3.rejected
        v4 = g.record_failure("t", args, error="boom")
        assert v4.tier is GuardrailTier.BLOCK and v4.rejected
        v5 = g.record_failure("t", args, error="boom")
        assert v5.tier is GuardrailTier.HALT and v5.rejected

    def test_block_only_same_args_halt_blocks_all(self):
        g = ToolGuardrails()
        args = {"q": "x"}
        for _ in range(3):
            g.record_failure("t", args, error="boom")
        # block：同参被拒，换参仍可试（「同参连打」才是无信息量动作）
        blocked = g.check("t", args)
        assert blocked is not None and blocked.rejected
        assert blocked.tier is GuardrailTier.BLOCK
        assert g.check("t", {"q": "other"}) is None
        # halt：全部参数被拒
        for _ in range(2):
            g.record_failure("t", args, error="boom")
        halted = g.check("t", {"q": "other"})
        assert halted is not None and halted.rejected
        assert halted.tier is GuardrailTier.HALT

    def test_tool_level_escalation_2_5_10(self):
        """变参失败：同参链不升级，只按 tool_failures 分级。"""
        g = ToolGuardrails()
        v1 = g.record_failure("t", {"i": 0}, error="e")
        assert v1.tier is GuardrailTier.OK
        v2 = g.record_failure("t", {"i": 1}, error="e")
        assert v2.tier is GuardrailTier.WARN and v2.tool_failures == 2
        for i in range(2, 4):
            g.record_failure("t", {"i": i}, error="e")
        v5 = g.record_failure("t", {"i": 4}, error="e")
        assert v5.tier is GuardrailTier.BLOCK and v5.tool_failures == 5
        for i in range(5, 9):
            g.record_failure("t", {"i": i}, error="e")
        v10 = g.record_failure("t", {"i": 9}, error="e")
        assert v10.tier is GuardrailTier.HALT and v10.tool_failures == 10

    def test_readonly_success_breaks_same_chain_keeps_tool_failures(self):
        g = ToolGuardrails()
        args = {"q": "x"}
        g.record_failure("t", args, error="boom")
        g.record_failure("t", args, error="boom")
        v = g.record_success("t", args, result={"a": 1}, mutation=False)
        # 同参连续链被打断；tool_failures 保留（读成功不能证明写通了）
        assert v.same_args_failures == 0
        assert v.tool_failures == 2
        assert g.check("t", args) is None, "只读成功后同参应放行"

    def test_mutation_success_clears_everything(self):
        g = ToolGuardrails()
        args = {"q": "x"}
        for _ in range(3):
            g.record_failure("t", args, error="boom")
        assert g.check("t", args) is not None, "前置：同参已被 block"
        v = g.record_success("t", args, result={"ok": 1}, mutation=True)
        assert v.tier is GuardrailTier.OK and v.advice == ""
        assert g.check("t", args) is None, "mutation 成功必须清空拦截"
        assert g.advice_for("t") is None, "mutation 成功后不应再有失败建议"

    def test_identical_result_warn_on_third_hit(self):
        g = ToolGuardrails()
        assert g.record_success("t", {}, result={"rows": []}).advice == ""
        v2 = g.record_success("t", {}, result={"rows": []})
        assert v2.tier is GuardrailTier.OK, "第 1 次重复仍放行（原文保留）"
        v3 = g.record_success("t", {}, result={"rows": []})
        assert v3.tier is GuardrailTier.WARN
        assert "字节级相同" in v3.advice, "轮询不豁免：第 2 次重复出建议"
        v4 = g.record_success("t", {}, result={"rows": [1]})
        assert v4.tier is GuardrailTier.OK and v4.advice == "", "结果变化归零"

    def test_advice_for_requires_failure_record(self):
        g = ToolGuardrails()
        assert g.advice_for("t") is None, "无记录 ⇒ None"
        g.record_failure("t", {}, error="TimeoutError: timeout after 3000ms")
        advice = g.advice_for("t")
        assert advice and "超时" in advice, "建议来自最近一次真实失败"
        assert g.advice_for("other") is None, "未跟踪工具无建议"

    @pytest.mark.parametrize(
        "error",
        [
            "TimeoutError: timeout after 1000ms",
            "PermissionError: permission denied",
            "ValueError: unknown tool foo",
            "ValueError: invalid argument",
            "RuntimeError: connection reset",
            "guardrail block",
            "guardrail halt",
            "repeated",
            "",
        ],
    )
    def test_advice_avoids_ascii_retry_hints(self, error):
        """恢复建议不得携带 is_retryable 的 ASCII 提示词（双保险）。"""
        for tool in ("t", "tool_with_400_name"):
            advice = recovery_advice(tool, error)
            assert advice
            for hint in _HINTS:
                assert hint not in advice.lower(), (
                    f"建议 {advice!r} 含提示词 {hint!r}（会污染 is_retryable）"
                )

    def test_lru_bound_evicts_oldest(self):
        g = ToolGuardrails(max_tools=3)
        for i in range(5):
            g.record_failure(f"t{i}", {}, error="e")
        assert g.advice_for("t0") is None, "超上限：最旧工具被淘汰"
        assert g.advice_for("t4") is not None


# ---------------------------------------------------------------------------
# 2. executor 接线（flag 门控 + error 文本不变 + metadata 回喂）
# ---------------------------------------------------------------------------


class TestExecutorGuardrailWiring:
    @pytest.mark.asyncio
    async def test_flag_off_failure_identical_to_baseline(self, monkeypatch):
        monkeypatch.setattr(settings, "TOOL_GUARDRAILS_ENABLED", False)
        handler = _ToggleTool()
        ex = _make_executor("t_p54_fail", handler)
        for _ in range(3):
            r = await ex.execute("t_p54_fail", {"q": 1})
            assert r.success is False
            assert r.error == "ValueError: boom", "error 文本逐位等价基线"
            assert "guardrail" not in r.metadata
        # flag 关：第 4 次同参照常执行（护栏从不拒绝）
        r4 = await ex.execute("t_p54_fail", {"q": 1})
        assert r4.success is False and r4.error == "ValueError: boom"
        assert handler.calls == 4

    @pytest.mark.asyncio
    async def test_flag_on_tiers_and_metadata(self, monkeypatch):
        monkeypatch.setattr(settings, "TOOL_GUARDRAILS_ENABLED", True)
        handler = _ToggleTool()
        ex = _make_executor("t_p54_fail", handler)
        args = {"q": 1}
        # 第 1 次失败：tier ok ⇒ 无建议、无 metadata
        r1 = await ex.execute("t_p54_fail", args)
        assert r1.error == "ValueError: boom"
        assert "guardrail" not in r1.metadata
        # 第 2 次：warn 建议进 metadata，error 原文不动
        r2 = await ex.execute("t_p54_fail", args)
        assert r2.error == "ValueError: boom", "建议不得拼进 error"
        assert r2.metadata["guardrail"]["tier"] == "warn"
        assert r2.metadata["guardrail"]["advice"]
        # 第 3 次：block 判级
        r3 = await ex.execute("t_p54_fail", args)
        assert r3.error == "ValueError: boom"
        assert r3.metadata["guardrail"]["tier"] == "block"
        # 第 4 次同参：前置拦截，handler 不再被调
        r4 = await ex.execute("t_p54_fail", args)
        assert r4.success is False
        assert r4.error.startswith("guardrail block: ")
        assert r4.metadata["reason"] == "guardrail_rejected"
        assert r4.retryable is False
        assert handler.calls == 3, "block 后 handler 不得再执行"
        # 换参：block 只拦同参，仍可执行
        r5 = await ex.execute("t_p54_fail", {"q": 2})
        assert r5.metadata.get("reason") != "guardrail_rejected"
        assert handler.calls == 4

    @pytest.mark.asyncio
    async def test_flag_on_halt_blocks_every_args(self, monkeypatch):
        monkeypatch.setattr(settings, "TOOL_GUARDRAILS_ENABLED", True)
        handler = _ToggleTool()
        ex = _make_executor("t_p54_fail", handler)
        g = get_tool_guardrails()
        for _ in range(5):
            g.record_failure("t_p54_fail", {"x": 1}, error="boom")
        r = await ex.execute("t_p54_fail", {"x": 999})
        assert r.success is False
        assert r.error.startswith("guardrail halt: ")
        assert handler.calls == 0, "halt 后任何参数都不执行"

    @pytest.mark.asyncio
    async def test_flag_on_mutation_success_clears_counters(self, monkeypatch):
        monkeypatch.setattr(settings, "TOOL_GUARDRAILS_ENABLED", True)
        handler = _ToggleTool(fail=True)
        ex = _make_executor("t_p54_ok", handler, side_effect=True)
        args = {"q": "x"}
        await ex.execute("t_p54_ok", args)  # 1 失败（未到 block）
        await ex.execute("t_p54_ok", args)  # 2 失败 = warn
        handler.fail = False
        ok = await ex.execute("t_p54_ok", args)
        assert ok.success is True
        assert "guardrail" not in ok.metadata, "mutation 成功清零 ⇒ 无建议"
        g = get_tool_guardrails()
        assert g.check("t_p54_ok", args) is None
        assert g.advice_for("t_p54_ok") is None

    @pytest.mark.asyncio
    async def test_flag_on_identical_success_metadata(self, monkeypatch):
        """只读工具连续成功返回同一结果 ⇒ 第 3 次起 metadata 出 warn 建议。"""
        monkeypatch.setattr(settings, "TOOL_GUARDRAILS_ENABLED", True)
        handler = lambda args: {"rows": []}  # noqa: E731 - 测试桩
        ex = _make_executor("t_p54_poll", handler)
        r1 = await ex.execute("t_p54_poll", {})
        r2 = await ex.execute("t_p54_poll", {})
        r3 = await ex.execute("t_p54_poll", {})
        assert "guardrail" not in r1.metadata
        assert "guardrail" not in r2.metadata, "第 1 次重复保留原文"
        assert r3.metadata["guardrail"]["tier"] == "warn"
        assert "字节级相同" in r3.metadata["guardrail"]["advice"]


# ---------------------------------------------------------------------------
# 3. task_retry 接线（前置 skip + 终局回喂）
# ---------------------------------------------------------------------------


class TestTaskRetryGuardrails:
    @pytest.mark.asyncio
    async def test_block_skip_never_starts_runner(self, monkeypatch):
        monkeypatch.setattr(settings, "TOOL_GUARDRAILS_ENABLED", True)
        g = get_tool_guardrails()
        args = {"q": "x"}
        for _ in range(3):
            g.record_failure("t_r", args, error="boom")
        calls: list = []

        async def runner(spec):
            calls.append(spec)
            return "ok"

        spec = PipelineTaskSpec(name="step-1", payload={"tool": "t_r", "args": dict(args)})
        out = await run_task(spec, runner, max_attempts=3, base_delay=0.01, jitter=False)
        assert out.ok is False
        assert out.skipped_reason == "guardrail_block"
        assert out.attempts == 0, "被拦下的调用不消耗重试预算"
        assert out.retryable is False
        assert out.error.startswith("guardrail block: ")
        assert calls == [], "block 时 runner 一次都不启动"

    @pytest.mark.asyncio
    async def test_halt_skip_never_starts_runner(self, monkeypatch):
        monkeypatch.setattr(settings, "TOOL_GUARDRAILS_ENABLED", True)
        g = get_tool_guardrails()
        for _ in range(5):
            g.record_failure("t_r", {"x": 1}, error="boom")
        calls: list = []

        async def runner(spec):
            calls.append(spec)
            return "ok"

        spec = PipelineTaskSpec(name="step-2", payload={"tool": "t_r", "args": {"x": 1}})
        out = await run_task(spec, runner, max_attempts=3, base_delay=0.01, jitter=False)
        assert out.ok is False
        assert out.skipped_reason == "guardrail_halt"
        assert out.attempts == 0 and out.retryable is False
        assert out.error.startswith("guardrail halt: ")
        assert calls == []

    @pytest.mark.asyncio
    async def test_terminal_failure_appends_advice(self, monkeypatch):
        monkeypatch.setattr(settings, "TOOL_GUARDRAILS_ENABLED", True)
        g = get_tool_guardrails()
        g.record_failure("t_term", {"q": 1}, error="TimeoutError: timeout after 1000ms")

        async def runner(spec):
            raise ValueError("boom")

        spec = PipelineTaskSpec(name="step-3", payload={"tool": "t_term", "args": {"q": 1}})
        out = await run_task(spec, runner, max_attempts=2, base_delay=0.01, jitter=False)
        assert out.ok is False and out.error_type == "ValueError"
        assert out.attempts == 1, "ValueError 不可重试：首个失败即终局"
        assert out.error.startswith("ValueError: boom")
        assert "恢复建议：" in out.error, "终局回喂：建议追加在分类之后"

    @pytest.mark.asyncio
    async def test_flag_off_terminal_error_unchanged(self, monkeypatch):
        monkeypatch.setattr(settings, "TOOL_GUARDRAILS_ENABLED", False)
        # 即便引擎里有失败记录，flag 关也绝不拼接
        get_tool_guardrails().record_failure(
            "t_term", {"q": 1}, error="TimeoutError: timeout after 1000ms"
        )

        async def runner(spec):
            raise ValueError("boom")

        spec = PipelineTaskSpec(name="step-4", payload={"tool": "t_term", "args": {"q": 1}})
        out = await run_task(spec, runner, max_attempts=2, base_delay=0.01, jitter=False)
        assert out.error == "ValueError: boom", "flag-off 与基线逐位等价"
        assert out.skipped_reason is None


# ---------------------------------------------------------------------------
# 4. RetryReplanner 回喂通道（rationale 拼接，异常安全）
# ---------------------------------------------------------------------------


class TestRetryReplannerAdvice:
    def test_without_guardrails_base_unchanged(self):
        r = RetryReplanner()
        assert r._advice_rationale("t", "replan after boom") == "replan after boom"

    def test_with_guardrails_appends_advice(self):
        g = ToolGuardrails()
        g.record_failure("t", {}, error="PermissionError: permission denied")
        r = RetryReplanner(guardrails=g)
        out = r._advice_rationale("t", "replan after fail")
        assert out.startswith("replan after fail")
        assert "恢复建议：" in out

    def test_no_failure_record_keeps_base(self):
        r = RetryReplanner(guardrails=ToolGuardrails())
        assert r._advice_rationale("t", "base") == "base"

    def test_no_tool_keeps_base(self):
        g = ToolGuardrails()
        g.record_failure("t", {}, error="boom")
        r = RetryReplanner(guardrails=g)
        assert r._advice_rationale(None, "base") == "base"
        assert r._advice_rationale("", "base") == "base"

    def test_broken_guardrails_keeps_base(self):
        class _Boom:
            def advice_for(self, tool):
                raise RuntimeError("engine down")

        r = RetryReplanner(guardrails=_Boom())
        assert r._advice_rationale("t", "base") == "base", "护栏故障不得影响重规划"


# ---------------------------------------------------------------------------
# 5. W2 重复结果折叠（引擎 + 两接点 flag 门控）
# ---------------------------------------------------------------------------


class TestResultDeduplicator:
    def test_third_identical_returns_stub(self):
        d = ResultDeduplicator()
        assert d.observe("tool_a", {"rows": [1]}) is None, "首次记录"
        assert d.observe("tool_a", {"rows": [1]}) is None, "第 1 次重复保留原文"
        stub = d.observe("tool_a", {"rows": [1]})
        assert stub is not None, "第 2 次字节级重复 ⇒ 折叠"
        assert "[重复结果已折叠]" in stub and "tool=tool_a" in stub
        assert d.observe("tool_a", {"rows": [2]}) is None, "结果变化重新记录"

    def test_tools_independent(self):
        d = ResultDeduplicator()
        d.observe("a", 1)
        d.observe("a", 1)
        assert d.observe("a", 1) is not None
        assert d.observe("b", 1) is None, "不同工具互不影响"

    def test_lru_evicts_oldest(self):
        d = ResultDeduplicator(max_tools=2)
        d.observe("a", 1)
        d.observe("a", 1)  # a repeats=1
        d.observe("b", 5)
        d.observe("b", 5)  # b repeats=1
        d.observe("c", 3)  # 触发淘汰最旧 = a
        # b 仍在 LRU：第 2 次字节级重复照常折叠
        assert d.observe("b", 5) is not None
        # a 已被淘汰 ⇒ 历史清零（若未淘汰，此刻 repeats 应到 2 出 stub）
        assert d.observe("a", 1) is None


class TestDedupWiring:
    @pytest.mark.asyncio
    async def test_mapping_executor_flag_on_stub(self, monkeypatch):
        monkeypatch.setattr(settings, "TOOL_GUARDRAILS_ENABLED", True)
        ex = MappingStepExecutor(tools={"poll": lambda **kw: {"v": 1}})
        task = TaskSpec(goal="g")
        plan = Plan(steps=(PlanStep(id="s1", action="poll", tool="poll"),))
        step = plan.steps[0]
        ctx = ContextSnapshot(prompt="")
        o1 = await ex.execute(task, plan, step, ctx)
        o2 = await ex.execute(task, plan, step, ctx)
        o3 = await ex.execute(task, plan, step, ctx)
        assert o1.ok and o2.ok
        assert "deduped" not in (o1.meta or {})
        assert o3.ok, "折叠不改变成功语义"
        assert (o3.meta or {}).get("deduped") is True
        assert o3.content.startswith("[重复结果已折叠]")

    @pytest.mark.asyncio
    async def test_mapping_executor_flag_off_no_dedup(self, monkeypatch):
        monkeypatch.setattr(settings, "TOOL_GUARDRAILS_ENABLED", False)
        ex = MappingStepExecutor(tools={"poll": lambda **kw: {"v": 1}})
        task = TaskSpec(goal="g")
        plan = Plan(steps=(PlanStep(id="s1", action="poll", tool="poll"),))
        step = plan.steps[0]
        ctx = ContextSnapshot(prompt="")
        for _ in range(3):
            o = await ex.execute(task, plan, step, ctx)
            assert o.ok
            assert "deduped" not in (o.meta or {})
            assert "[重复结果已折叠]" not in o.content

    def test_to_observation_flag_on_stub(self, monkeypatch):
        from agentix.application.services.task_execution import _to_observation

        monkeypatch.setattr(settings, "TOOL_GUARDRAILS_ENABLED", True)
        dedup = get_result_deduplicator()
        value = {"rows": [1]}
        assert dedup.observe("kb_query", value) is None
        assert dedup.observe("kb_query", value) is None
        spec = PipelineTaskSpec(name="w1", payload={"tool": "kb_query"})
        outcome = TaskOutcome(name="w1", ok=True, value=value, attempts=1)
        obs = _to_observation(spec, outcome, wave=1)
        assert obs.ok
        assert obs.meta.get("deduped") is True
        assert obs.content.startswith("[重复结果已折叠]")

    def test_to_observation_flag_off_ignores_engine_state(self, monkeypatch):
        from agentix.application.services.task_execution import _to_observation

        monkeypatch.setattr(settings, "TOOL_GUARDRAILS_ENABLED", False)
        # 即便引擎里已有可折叠历史，flag 关也绝不观察
        dedup = get_result_deduplicator()
        value = {"rows": [1]}
        dedup.observe("kb_query", value)
        dedup.observe("kb_query", value)
        spec = PipelineTaskSpec(name="w1", payload={"tool": "kb_query"})
        outcome = TaskOutcome(name="w1", ok=True, value=value, attempts=1)
        obs = _to_observation(spec, outcome, wave=1)
        assert "deduped" not in obs.meta
        assert "[重复结果已折叠]" not in obs.content


# ---------------------------------------------------------------------------
# 7. 装配卫生：flag-off 时声明清单不含计划态工具（防串味）
# ---------------------------------------------------------------------------


def test_flag_off_declarations_contain_no_new_tools():
    registry_init._DECLARATIONS.clear()
    try:
        names = [spec.name for spec, _ in registry_init.get_declarations()]
        assert "enter_plan_mode" not in names
        assert "exit_plan_mode" not in names
    finally:
        registry_init._DECLARATIONS.clear()
