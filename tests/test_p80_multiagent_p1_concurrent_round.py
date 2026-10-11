"""F1d 验收（统一执行链 Stage F · MultiAgent P1 · A-P1-3 并发工具轮）。

覆盖 multiagent-optimization §5 的 A-P1-3：

1. **五处同步棘轮**（settings / selfcheck / .env.example / configuration.md /
   p41 手写清单由既有 p41 守卫），新键 ``AGENT_TOOL_ROUND_CONCURRENCY``
   默认 1（串行 = 与基线逐位等价）、``ge=1``、retrieval 侧透明委托。
2. **round_gate 共享闸**：``is_dangerous``（有效风险 MEDIUM+ = L1+，含
   参数提权）与 ``preflight_block``（执行点授权门：关闭零副作用恒放行、
   启用未获批 fail-closed 返回阻断消息）。
3. **Surface A（L2 检索聊天 ``_execute_tool_calls``）**：flag-off 串行原路
   逐位等价；flag-on 授权门 → 危险强制串行（输入序）→ safe 限并发 →
   结果按输入序组装；缺工具/异常/未获批均落 error ToolMessage。
4. **Surface B（L3 ``AgentLoop`` 批路径）**：flag-off 单步原路径不动；flag-on
   ready 批发（依赖不与前置同批）、剩余步数预算裁剪、危险单例批、授权门
   拦截步落 failed Observation 不派发、声明序落位 + ``steps_used`` 单调 +
   ``ON_STEP_END``、每批至多一次 replan、批前至多一次上下文重建。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional

import pytest
from pydantic import ValidationError

from agentix.agent import AgentLoop
from agentix.config.selfcheck import DEFAULT_IGNORED_MISSING
from agentix.config.settings import Settings, settings
from agentix.domain.agent.types import (
    ContextSnapshot,
    Observation,
    Plan,
    PlanStep,
    TaskSpec,
)
from agentix.orchestration import orchestrator as orch_mod
from agentix.policies.planner import DeclaredStepsPlanner
from agentix.retrieval.settings import settings as rs_settings
from agentix.retrieval.usecases import chat_agent_runner as runner
from agentix.tools.gateway.human_review import reset_review_gate
from agentix.tools.gateway import round_gate

REPO = Path(__file__).resolve().parents[1]

#: F1d 新键（默认 1 = 串行——任何测试都必须还原）
_KEY = "AGENT_TOOL_ROUND_CONCURRENCY"
assert getattr(settings, _KEY) == 1, f"{_KEY} 基线必须为 1"

#: 危险名（走 _NAME_PATTERNS：delete → CRITICAL）与安全名（无模式命中）
_DANGEROUS_TOOL = "delete_records"
_SAFE_TOOL = "search_documents"


@pytest.fixture(autouse=True)
def fresh_state():
    """review gate 单例复位（teardown 同款，防跨用例污染）。"""
    reset_review_gate()
    yield
    reset_review_gate()


# ---------------------------------------------------------------------------
# 1) 五处同步棘轮
# ---------------------------------------------------------------------------
class TestFlagSync:
    def test_key_default_is_one_and_clamped(self):
        assert settings.AGENT_TOOL_ROUND_CONCURRENCY == 1
        field = Settings.model_fields[_KEY]
        assert field.default == 1
        assert any(getattr(m, "ge", None) == 1 for m in field.metadata), "ge=1 约束必须存在"
        with pytest.raises(ValidationError):
            Settings(**{_KEY: 0})
        # AgentLoop 侧同样钳到 >=1（0/负数 ⇒ 串行）
        assert AgentLoop(planner=None, context=None, verifier=None, executor=None)._round_concurrency == 1

    def test_retrieval_settings_delegates_key(self):
        assert rs_settings.AGENT_TOOL_ROUND_CONCURRENCY == 1

    def test_env_example_has_line(self):
        text = (REPO / ".env.example").read_text(encoding="utf-8")
        assert f"{_KEY}=1" in text

    def test_configuration_md_section2_and_section5(self):
        text = (REPO / "docs" / "architecture" / "configuration.md").read_text(encoding="utf-8")
        assert f"`{_KEY}`" in text, "§2 Agent 行为行缺键"
        assert f"| `{_KEY}` | `1` |" in text, "§5 per-key 行缺键"

    def test_selfcheck_ignores_key(self):
        assert _KEY in DEFAULT_IGNORED_MISSING

    def test_adapter_injects_round_concurrency(self):
        src = (REPO / "agentix" / "application" / "services" / "agent_loop_adapter.py").read_text(
            encoding="utf-8"
        )
        assert "round_concurrency=int(settings.AGENT_TOOL_ROUND_CONCURRENCY)" in src


# ---------------------------------------------------------------------------
# 2) round_gate 共享闸
# ---------------------------------------------------------------------------
class TestRoundGate:
    def test_is_dangerous_safe_names_false(self):
        for name in (_SAFE_TOOL, "count_scope_documents", "lookup_scope_document"):
            assert round_gate.is_dangerous(name) is False

    def test_is_dangerous_pattern_names_true(self):
        # delete→CRITICAL、run→HIGH、sync→MEDIUM（均 ∈ REVIEW_RISK_LEVELS = L1+）
        for name in (_DANGEROUS_TOOL, "run_shell", "sync_kb"):
            assert round_gate.is_dangerous(name) is True

    def test_is_dangerous_arg_escalation(self):
        assert round_gate.is_dangerous("fetch_cache") is False
        assert round_gate.is_dangerous("fetch_cache", {"force": True}) is True

    def test_is_dangerous_empty_name_false(self):
        assert round_gate.is_dangerous("") is False

    def test_preflight_disabled_passthrough(self):
        # HUMAN_REVIEW 关（基线）⇒ 闸门第一行返回，恒 None、零副作用
        assert round_gate.preflight_block(_DANGEROUS_TOOL) is None

    def test_preflight_blocks_dangerous_when_enabled(self, monkeypatch):
        monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", True, raising=False)
        reset_review_gate()
        msg = round_gate.preflight_block(_DANGEROUS_TOOL)
        assert msg is not None
        assert "human review required" in msg
        assert _DANGEROUS_TOOL in msg

    def test_preflight_allows_safe_when_enabled(self, monkeypatch):
        monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", True, raising=False)
        reset_review_gate()
        assert round_gate.preflight_block(_SAFE_TOOL) is None


# ---------------------------------------------------------------------------
# 并发探针（两面共用）：max_active / 调用序 / 危险重叠
# ---------------------------------------------------------------------------
class _Flight:
    def __init__(self, danger_name: Optional[str] = None) -> None:
        self.active = 0
        self.max_active = 0
        self.order: List[str] = []
        self.danger_name = danger_name
        self.danger_active = False
        self.overlap_danger: List[str] = []

    def start(self, name: str) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.order.append(name)
        if self.danger_name == name:
            self.danger_active = True
        elif self.danger_active:
            self.overlap_danger.append(name)

    def end(self, name: str) -> None:
        self.active -= 1
        if self.danger_name == name:
            self.danger_active = False


class _FakeTool:
    """Surface A 桩工具：``.ainvoke(tc)`` 与生产语义同款返回。"""

    def __init__(self, name: str, flight: _Flight, *, delay: float = 0.02, fail: bool = False):
        self.name = name
        self.flight = flight
        self.delay = delay
        self.fail = fail

    async def ainvoke(self, tc):
        self.flight.start(self.name)
        try:
            await asyncio.sleep(self.delay)
            if self.fail:
                raise RuntimeError("boom")
            return SimpleNamespace(content=f"ok-{self.name}")
        finally:
            self.flight.end(self.name)


def _tc(call_id: str, name: str, args: Optional[dict] = None) -> dict:
    import json

    return {
        "id": call_id,
        "name": name,
        "arguments": json.dumps(args or {"q": "x"}),
    }


# ---------------------------------------------------------------------------
# 3) Surface A：L2 检索聊天 _execute_tool_calls
# ---------------------------------------------------------------------------
class TestSurfaceAChatLoop:
    @pytest.mark.asyncio
    async def test_flag_off_serial_baseline(self):
        flight = _Flight()
        tools = {
            "a": _FakeTool("a", flight),
            "b": _FakeTool("b", flight),
        }
        outs = await runner._execute_tool_calls([_tc("c1", "a"), _tc("c2", "b")], tools)
        assert flight.max_active == 1, "flag-off 必须串行"
        assert flight.order == ["a", "b"]
        assert [o.content for o in outs] == ["ok-a", "ok-b"]

    @pytest.mark.asyncio
    async def test_flag_on_two_safe_overlap(self, monkeypatch):
        monkeypatch.setattr(settings, _KEY, 2)
        flight = _Flight()
        tools = {"a": _FakeTool("a", flight), "b": _FakeTool("b", flight)}
        outs = await runner._execute_tool_calls([_tc("c1", "a"), _tc("c2", "b")], tools)
        assert flight.max_active == 2, "flag-on 两个安全工具必须并发"
        assert [o.content for o in outs] == ["ok-a", "ok-b"], "结果必须按输入序组装"

    @pytest.mark.asyncio
    async def test_flag_on_semaphore_cap(self, monkeypatch):
        monkeypatch.setattr(settings, _KEY, 2)
        flight = _Flight()
        tools = {n: _FakeTool(n, flight) for n in ("t1", "t2", "t3", "t4")}
        calls = [_tc(f"c{i}", n) for i, n in enumerate(("t1", "t2", "t3", "t4"))]
        outs = await runner._execute_tool_calls(calls, tools)
        assert len(outs) == 4
        assert flight.max_active == 2, "Semaphore 必须把并发封在 2"
        assert set(flight.order) == {"t1", "t2", "t3", "t4"}

    @pytest.mark.asyncio
    async def test_dangerous_serialized_first_no_overlap(self, monkeypatch):
        monkeypatch.setattr(settings, _KEY, 3)
        flight = _Flight(danger_name=_DANGEROUS_TOOL)
        tools = {
            "s1": _FakeTool("s1", flight),
            _DANGEROUS_TOOL: _FakeTool(_DANGEROUS_TOOL, flight),
            "s2": _FakeTool("s2", flight),
        }
        calls = [_tc("c1", "s1"), _tc("c2", _DANGEROUS_TOOL), _tc("c3", "s2")]
        outs = await runner._execute_tool_calls(calls, tools)
        assert flight.order[0] == _DANGEROUS_TOOL, "危险调用先串行执行"
        assert flight.overlap_danger == [], "危险调用绝不与任何调用并发"
        assert flight.max_active == 2, "其余安全调用仍应并发"
        # 输出仍按输入序（危险在输入中间）
        assert [o.content for o in outs] == ["ok-s1", f"ok-{_DANGEROUS_TOOL}", "ok-s2"]

    @pytest.mark.asyncio
    async def test_gate_blocks_dangerous_call(self, monkeypatch):
        monkeypatch.setattr(settings, _KEY, 2)
        monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", True, raising=False)
        reset_review_gate()
        flight = _Flight()
        tools = {_SAFE_TOOL: _FakeTool(_SAFE_TOOL, flight), _DANGEROUS_TOOL: _FakeTool(_DANGEROUS_TOOL, flight)}
        outs = await runner._execute_tool_calls(
            [_tc("c1", _SAFE_TOOL), _tc("c2", _DANGEROUS_TOOL)], tools
        )
        assert flight.order == [_SAFE_TOOL], "未获批的危险调用不得派发"
        assert outs[0].content == f"ok-{_SAFE_TOOL}"
        assert "human review required" in outs[1].content
        assert outs[1].status == "error"

    @pytest.mark.asyncio
    async def test_missing_tool_error_message(self, monkeypatch):
        monkeypatch.setattr(settings, _KEY, 2)
        flight = _Flight()
        tools = {_SAFE_TOOL: _FakeTool(_SAFE_TOOL, flight)}
        outs = await runner._execute_tool_calls(
            [_tc("c1", _SAFE_TOOL), _tc("c2", "ghost_tool")], tools
        )
        assert outs[0].content == f"ok-{_SAFE_TOOL}"
        assert "工具 ghost_tool 不存在" in outs[1].content
        assert outs[1].status == "error"

    @pytest.mark.asyncio
    async def test_tool_failure_error_message(self, monkeypatch):
        monkeypatch.setattr(settings, _KEY, 2)
        flight = _Flight()
        tools = {"bad": _FakeTool("bad", flight, fail=True), "ok": _FakeTool("ok", flight)}
        outs = await runner._execute_tool_calls([_tc("c1", "bad"), _tc("c2", "ok")], tools)
        assert outs[0].status == "error"
        assert "工具执行失败" in outs[0].content
        assert outs[1].content == "ok-ok"

    def test_tool_call_args_parsing(self):
        assert runner._tool_call_args(_tc("c1", "x", {"q": "hi"})) == {"q": "hi"}
        assert runner._tool_call_args(
            {"id": "c2", "function": {"name": "x", "arguments": '{"limit": 3}'}}
        ) == {"limit": 3}
        assert runner._tool_call_args({"id": "c3", "name": "x", "arguments": "not-json"}) == {}
        assert runner._tool_call_args("not-a-dict") == {}


# ---------------------------------------------------------------------------
# 4) Surface B：L3 AgentLoop 批路径
# ---------------------------------------------------------------------------
class _ProbeExecutor:
    """批路径执行探针：并发计数 + 调用序 + 按 tool 名选择失败。"""

    def __init__(self, flight: _Flight, *, boom_tools: tuple = ()) -> None:
        self.flight = flight
        self.boom_tools = boom_tools
        self.calls: List[str] = []

    async def execute(self, task, plan, step, context) -> Observation:
        self.calls.append(step.id)
        self.flight.start(step.tool or step.action)
        try:
            await asyncio.sleep(0.02)
            if (step.tool or step.action) in self.boom_tools:
                raise RuntimeError("boom")
            return Observation(step_id=step.id, content=f"ok-{step.id}")
        finally:
            self.flight.end(step.tool or step.action)


class _PassVerifier:
    async def verify(self, task, plan, observations):
        from agentix.domain.agent.types import VerificationResult

        return VerificationResult(passed=True, reason="pass")


class _CountingContext:
    def __init__(self) -> None:
        self.builds = 0

    async def build(self, task, *, trajectory=(), budget_tokens=0):
        self.builds += 1
        return ContextSnapshot(prompt=f"ctx-{self.builds}")


class _CountingReplanner:
    def __init__(self, new_plan: Optional[Plan]) -> None:
        self.calls = 0
        self.new_plan = new_plan

    async def replan(self, task, plan, context, observations, *, last_failure=None):
        self.calls += 1
        return self.new_plan if self.calls == 1 else None


def _steps(*specs: tuple) -> dict:
    """specs = (id, tool) 或 (id, tool, depends_on)。"""
    out = []
    for spec in specs:
        sid, tool = spec[0], spec[1]
        deps = spec[2] if len(spec) > 2 else []
        out.append({"id": sid, "action": "act", "tool": tool, "depends_on": deps})
    return {"steps": out}


def _task(steps: dict) -> TaskSpec:
    return TaskSpec(goal="g", task_id="p80", constraints=steps)


def _loop(flight: _Flight, *, conc: int = 1, max_steps: int = 10, **kw) -> AgentLoop:
    return AgentLoop(
        planner=DeclaredStepsPlanner(),
        context=kw.pop("context", None) or _CountingContext(),
        verifier=_PassVerifier(),
        executor=_ProbeExecutor(flight, **kw),
        max_steps=max_steps,
        round_concurrency=conc,
    )


class TestSurfaceBAgentLoop:
    @pytest.mark.asyncio
    async def test_default_serial_baseline(self):
        flight = _Flight()
        loop = _loop(flight, conc=1)
        result = await loop.run(_task(_steps(("s1", "a"), ("s2", "b"))))
        assert flight.max_active == 1, "round_concurrency=1 必须串行"
        assert flight.order == ["a", "b"]
        assert result.terminal == "completed"
        assert result.steps_used == 2

    @pytest.mark.asyncio
    async def test_batch_two_independent_overlaps(self):
        flight = _Flight()
        loop = _loop(flight, conc=2)
        result = await loop.run(_task(_steps(("s1", "a"), ("s2", "b"))))
        assert flight.max_active == 2, "独立两步必须同批并发"
        assert [o.step_id for o in result.observations] == ["s1", "s2"]
        assert result.steps_used == 2
        assert result.terminal == "completed"

    @pytest.mark.asyncio
    async def test_semaphore_cap_batch4_conc2(self):
        flight = _Flight()
        loop = _loop(flight, conc=2)
        result = await loop.run(
            _task(_steps(("s1", "a"), ("s2", "b"), ("s3", "c"), ("s4", "d")))
        )
        assert flight.max_active == 2, "Semaphore 必须把批并发封在 2"
        assert [o.step_id for o in result.observations] == ["s1", "s2", "s3", "s4"]
        assert result.steps_used == 4

    @pytest.mark.asyncio
    async def test_dependency_not_batched_with_prerequisite(self):
        flight = _Flight()
        loop = _loop(flight, conc=3)
        result = await loop.run(_task(_steps(("s1", "a"), ("s2", "b", ["s1"]), ("s3", "c"))))
        # 第 1 批 = [s1, s3]（s2 依赖未满足不得入批）；第 2 批 = [s2]
        assert flight.max_active == 2
        assert [o.step_id for o in result.observations] == ["s1", "s3", "s2"]
        assert flight.order == ["a", "c", "b"], "依赖步必须在前置完成后执行"
        assert result.steps_used == 3

    @pytest.mark.asyncio
    async def test_budget_clip_max_steps1(self):
        flight = _Flight()
        loop = _loop(flight, conc=2, max_steps=1)
        result = await loop.run(_task(_steps(("s1", "a"), ("s2", "b"))))
        assert result.terminal == "budget_exceeded"
        assert result.steps_used == 1, "并发不得放宽步数预算"
        assert flight.order == ["a"], "批宽必须裁到剩余预算"

    @pytest.mark.asyncio
    async def test_danger_singleton_first_round_then_batch(self):
        flight = _Flight(danger_name=_DANGEROUS_TOOL)
        loop = _loop(flight, conc=3)
        result = await loop.run(
            _task(_steps(("d", _DANGEROUS_TOOL), ("s1", "a"), ("s2", "b")))
        )
        assert flight.order[0] == _DANGEROUS_TOOL, "危险步单例批先执行"
        assert flight.overlap_danger == [], "危险步绝不与任何步并发"
        assert flight.max_active == 2, "下一批安全步必须并发"
        assert [o.step_id for o in result.observations] == ["d", "s1", "s2"]
        assert result.steps_used == 3

    @pytest.mark.asyncio
    async def test_gate_blocked_step_failed_obs_no_exec(self, monkeypatch):
        monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", True, raising=False)
        reset_review_gate()
        flight = _Flight()
        loop = _loop(flight, conc=2)
        result = await loop.run(_task(_steps(("d", _DANGEROUS_TOOL))))
        assert loop._executor.calls == [], "被拦步不得派发"
        assert len(result.observations) == 1
        obs = result.observations[0]
        assert obs.ok is False
        assert obs.error_code == "review_required"
        assert "human review required" in obs.content
        assert result.steps_used == 1, "被拦步消耗一次尝试"

    @pytest.mark.asyncio
    async def test_mixed_blocked_and_exec_declaration_order(self, monkeypatch):
        monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", True, raising=False)
        reset_review_gate()
        flight = _Flight()
        loop = _loop(flight, conc=2)
        result = await loop.run(_task(_steps(("d", _DANGEROUS_TOOL), ("s1", "a"))))
        assert loop._executor.calls == ["s1"]
        assert [o.step_id for o in result.observations] == ["d", "s1"], "按声明序落位"
        assert result.observations[0].ok is False
        assert result.observations[1].ok is True
        assert result.steps_used == 2

    @pytest.mark.asyncio
    async def test_step_end_indexes_monotonic(self, monkeypatch):
        events: List[dict] = []
        monkeypatch.setattr(orch_mod, "emit", lambda hook, payload: events.append((hook, payload)))
        flight = _Flight()
        loop = _loop(flight, conc=2)
        result = await loop.run(_task(_steps(("s1", "a"), ("s2", "b"))))
        step_ends = [p for h, p in events if h == "on_step_end"]
        assert [p["index"] for p in step_ends] == [1, 2], "steps_used 单调 + 单步一事件"
        assert [p["step_id"] for p in step_ends] == [o.step_id for o in result.observations]

    @pytest.mark.asyncio
    async def test_one_replan_per_batch(self):
        flight = _Flight()
        new_plan = Plan(steps=(PlanStep(id="x", action="act", tool="ok"),))
        rp = _CountingReplanner(new_plan)
        loop = _loop(flight, conc=2, boom_tools=("boom",))
        loop._replanner = rp
        loop._max_replans = 3
        result = await loop.run(_task(_steps(("b1", "boom"), ("b2", "boom"))))
        assert rp.calls == 1, "同批两个失败只允许至多一次 replan"
        assert result.replans == 1
        assert result.terminal == "completed", "重规划后新计划继续执行"
        assert result.steps_used == 3

    @pytest.mark.asyncio
    async def test_context_rebuild_once_per_batch(self):
        flight = _Flight()
        ctx = _CountingContext()
        loop = _loop(flight, conc=2, context=ctx)
        loop._context_budget = 100
        # 依赖链强制两轮：轮1 无观测不重建、轮2 批前重建恰一次
        result = await loop.run(_task(_steps(("s1", "a"), ("s2", "b", ["s1"]))))
        assert result.terminal == "completed"
        # 初始构建 1 次 + 第 2 轮（有观测后）批前重建 1 次 = 2（不逐步重建）
        assert ctx.builds == 2

    @pytest.mark.asyncio
    async def test_batch_exception_classified_as_tool_failure(self):
        flight = _Flight()
        loop = _loop(flight, conc=2, boom_tools=("boom",))
        result = await loop.run(_task(_steps(("b1", "boom"), ("s1", "a"))))
        bad = next(o for o in result.observations if o.step_id == "b1")
        assert bad.ok is False
        assert bad.failure_layer is not None and bad.failure_layer.value == "tool"
        assert bad.error_code == "tool_exec_failed"
        assert any(o.step_id == "s1" and o.ok for o in result.observations), "同批成功步不受影响"
