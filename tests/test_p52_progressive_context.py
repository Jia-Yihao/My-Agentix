"""P3 渐进上下文验收测试（岗位对照·上下文生命周期，借鉴 claw-code §5 行 363+）。

覆盖四个 workstream 的 flag 治理与行为契约：

1. **指令层预算纪律（W1，借鉴 3.1）**：内容 hash 去重（保首次出现）+
   总预算 ``TOTAL_INSTRUCTION_CHARS``（尾部高优先级优先）；不重复、
   合计预算内的输入与既有输出**逐字节一致**（零新 settings 键）。
2. **L4 渐进压缩阶梯（W2，借鉴 3.2）**：``CONTEXT_COMPACT_LADDER``
   默认 False = Phase 22 单次语义逐字节不变；flag-on = keep-last
   窗口阶梯 4→2→1→0（空/五要素被拒换更小窗口；异常立即回退；
   四轮耗尽回退丢弃循环——确定性终止）。
3. **主循环渐进重建（W3，补「一次性构建 budget=0」缺口）**：
   ``AGENT_CONTEXT_BUDGET_TOKENS=0``（默认）单次构建与基线逐位等价；
   >0 = 每步按累计观测重建 + 预算裁剪；重建异常 ⇒ context_build_failed
   终态不 hang；组合根注入（AgentLoop 不 import config）。
4. **诊断只读契约（W5，借鉴 3.6）**：``context_report()`` 纯函数、
   JSON 可序列化、不走 provider/metrics 通道；``/ops/context`` 已注册。
"""

from __future__ import annotations

import json

import pytest

from agentix.agent.context import ContextEngine, SimpleContextBuilder, build_instructions
from agentix.agent.context.compaction import (
    COMPACT_LADDER_ROUNDS,
    ladder_enabled,
    maybe_compact,
)
from agentix.agent.context.instructions import TOTAL_INSTRUCTION_CHARS
from agentix.orchestration.orchestrator import AgentLoop, LoopResult
from agentix.application.services import agent_loop_adapter
from agentix.application.services.observability import context_report
from agentix.config.settings import settings
from agentix.domain.agent.types import (
    ContextSnapshot,
    Observation,
    Plan,
    PlanStep,
    TaskSpec,
)

# 新键基线（默认关/0——任何测试都必须还原）
assert settings.CONTEXT_COMPACT_LADDER is False, "CONTEXT_COMPACT_LADDER 基线必须为 False"
assert settings.AGENT_CONTEXT_BUDGET_TOKENS == 0, "AGENT_CONTEXT_BUDGET_TOKENS 基线必须为 0"


def _task(goal: str = "G") -> TaskSpec:
    return TaskSpec(goal=goal, task_id="t52")


def _traj(n: int = 8) -> list[Observation]:
    """n 行可观测轨迹；每行含 trace_id 五要素 token、可分辨行号且足够长
    （单行 ~180 chars ⇒ 3 行即超 budget=80 的引擎测试阈值）。"""
    return [
        Observation(
            step_id=f"s{i}",
            content=f"trace_id=t{i} constraint=c{i} line{i} " + "word " * 40,
        )
        for i in range(1, n + 1)
    ]


class _LadderFake:
    """可编程压缩器：按调用序返回结果列表（末项重复）；记录每次入参。"""

    def __init__(self, results: list) -> None:
        self.calls = 0
        self.seen: list[str] = []
        self._results = list(results)

    async def compact(self, text: str, *, budget_tokens: int) -> str:
        out = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        self.seen.append(text)
        if isinstance(out, Exception):
            raise out
        return out


# ---------------------------------------------------------------------------
# W1：指令层去重 + 总预算
# ---------------------------------------------------------------------------


class TestInstructionsBudget:
    def test_baseline_flags_unchanged(self):
        # 新机制对不重复、预算内输入为 no-op —— 与既有三层序语义一致
        blocks = build_instructions(
            settings_rules=("S1", ""),
            load_project=False,
            session_rules=("SES",),
            skills_enabled=False,
        )
        assert blocks == ("S1", "SES")

    def test_typical_input_byte_identical(self):
        big = "x" * 4000  # 单块 4k：远低于 12k 总预算
        blocks = build_instructions(
            settings_rules=(big,),
            load_project=False,
            session_rules=("SES",),
            skills_enabled=False,
        )
        assert blocks == (big, "SES")

    def test_duplicate_blocks_deduped_keep_first(self):
        blocks = build_instructions(
            settings_rules=("SAME-RULE", "SAME-RULE"),
            load_project=False,
            session_rules=("SAME-RULE",),
            skills_enabled=False,
        )
        assert blocks == ("SAME-RULE",)

    def test_dedup_keeps_first_position_stable_prefix(self):
        # settings 层（前缀首块）与 session 层同内容 ⇒ 只留 settings 位置
        blocks = build_instructions(
            settings_rules=("X",),
            load_project=False,
            session_rules=("X", "Y"),
            skills_enabled=False,
        )
        assert blocks == ("X", "Y")

    def test_over_budget_keeps_high_priority_tail(self):
        settings_rule = "s" * 13000  # 超 12k 总预算
        blocks = build_instructions(
            settings_rules=(settings_rule,),
            load_project=False,
            session_rules=("SESSION-KEPT",),
            skills_enabled=False,
        )
        assert blocks[-1] == "SESSION-KEPT", "会话层（高优先级）必须完整保留"
        head = blocks[0]
        assert head.startswith("s" * 1000), "settings 层应保留尽量多的前缀"
        assert "已截断" in head, "低优先级块截断必须显式提示（L1 不静默）"
        total = sum(len(b) + 1 for b in blocks)
        assert total <= TOTAL_INSTRUCTION_CHARS

    def test_total_budget_noop_at_boundary(self):
        # 合计刚好不超预算 ⇒ 逐字节原样
        rule = "b" * (TOTAL_INSTRUCTION_CHARS - 10)
        blocks = build_instructions(
            settings_rules=(rule,),
            load_project=False,
            skills_enabled=False,
        )
        assert blocks == (rule,)

    def test_budget_constant_is_12k(self):
        assert TOTAL_INSTRUCTION_CHARS == 12000

    def test_project_layer_4k_still_applies_first(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("p" * 6000, encoding="utf-8")
        blocks = build_instructions(
            load_project=True,
            project_start=tmp_path,
            stop=tmp_path,
            skills_enabled=False,
        )
        # 单文件 4k 先截，再过 12k 总预算 —— 两道防线同 claw-code 4k/12k
        assert len(blocks) == 1
        assert "已截断" in blocks[0]
        assert len(blocks[0]) <= 4000 + len("\n…[项目指令已截断]")


# ---------------------------------------------------------------------------
# W2：L4 渐进压缩阶梯
# ---------------------------------------------------------------------------


class TestCompactionLadder:
    def test_flags_default_off(self):
        assert settings.CONTEXT_COMPACT_LADDER is False
        assert ladder_enabled() is False

    def test_rounds_constant(self):
        assert COMPACT_LADDER_ROUNDS == (4, 2, 1, 0)

    @pytest.mark.asyncio
    async def test_ladder_off_single_attempt_unchanged(self, monkeypatch):
        # ladder=False（默认）⇒ Phase 22 单次语义：恰好一次尝试
        monkeypatch.setattr(settings, "CONTEXT_AUTOCOMPACT", True)
        fake = _LadderFake(["trace_id=t1 t2 t3 condensed"])
        out = await maybe_compact(["trace_id=t1 line1", "line2"], fake, 10_000)
        assert fake.calls == 1
        assert out is not None

    @pytest.mark.asyncio
    async def test_ladder_requires_autocompact(self, monkeypatch):
        monkeypatch.setattr(settings, "CONTEXT_AUTOCOMPACT", False)
        monkeypatch.setattr(settings, "CONTEXT_COMPACT_LADDER", True)
        fake = _LadderFake(["x"])
        assert await maybe_compact(["trace_id=t1"], fake, 10_000) is None
        assert fake.calls == 0

    @pytest.mark.asyncio
    async def test_reject_then_accept_shrinks_window(self, monkeypatch):
        monkeypatch.setattr(settings, "CONTEXT_AUTOCOMPACT", True)
        monkeypatch.setattr(settings, "CONTEXT_COMPACT_LADDER", True)

        class _Scripted:
            """首轮拒绝（丢五要素），次轮 identity（恒过断言）。"""

            def __init__(self):
                self.calls = 0
                self.seen: list[str] = []

            async def compact(self, text: str, *, budget_tokens: int) -> str:
                self.calls += 1
                self.seen.append(text)
                if self.calls == 1:
                    return "bland text without markers"
                return text  # identity：恒过五要素

        scripted = _Scripted()
        traj = [o.content for o in _traj(8)]
        out = await maybe_compact(traj, scripted, 10_000)  # 大预算：接受即返回
        assert scripted.calls == 2, "首轮被拒 ⇒ 换 keep=2 窗口再试"
        assert len(scripted.seen[0].splitlines()) == 4, "首轮只压缩头部 4 行"
        assert len(scripted.seen[1].splitlines()) == 6, "次轮 keep=2 ⇒ 头部 6 行"
        assert out is not None
        text = "\n".join(out)
        assert "line7" in text and "line8" in text, "keep=2 尾部 2 行原样保留"

    @pytest.mark.asyncio
    async def test_exception_falls_back_immediately(self, monkeypatch):
        monkeypatch.setattr(settings, "CONTEXT_AUTOCOMPACT", True)
        monkeypatch.setattr(settings, "CONTEXT_COMPACT_LADDER", True)
        fake = _LadderFake([RuntimeError("llm down")])
        out = await maybe_compact([o.content for o in _traj()], fake, 10_000)
        assert fake.calls == 1, "provider 异常 ⇒ 立即回退，不空转四轮"
        assert out is None

    @pytest.mark.asyncio
    async def test_still_over_budget_retries_until_keep0(self, monkeypatch):
        monkeypatch.setattr(settings, "CONTEXT_AUTOCOMPACT", True)
        monkeypatch.setattr(settings, "CONTEXT_COMPACT_LADDER", True)
        traj = [o.content for o in _traj(6)]

        class _IdentityAlways:
            """每轮 identity（恒过五要素断言）——专测「仍超预算 ⇒ 换窗口」。"""

            def __init__(self):
                self.calls = 0

            async def compact(self, text: str, *, budget_tokens: int) -> str:
                self.calls += 1
                return text

        identity = _IdentityAlways()
        out = await maybe_compact(traj, identity, 1)  # budget=1 ⇒ 必然超
        assert identity.calls == 4, "超预算 ⇒ 4→2→1→0 全阶梯走满"
        assert out == traj, "keep=0 identity ⇒ 原轨迹交还，丢弃循环收尾"

    @pytest.mark.asyncio
    async def test_all_rejected_exhausts_then_none(self, monkeypatch):
        monkeypatch.setattr(settings, "CONTEXT_AUTOCOMPACT", True)
        monkeypatch.setattr(settings, "CONTEXT_COMPACT_LADDER", True)
        fake = _LadderFake(["bland text without markers"])
        out = await maybe_compact([o.content for o in _traj()], fake, 10_000)
        assert fake.calls == 4, "四轮耗尽（确定性上界）"
        assert out is None, "全部被拒 ⇒ 回退丢弃循环"

    @pytest.mark.asyncio
    async def test_empty_output_moves_to_next_round(self, monkeypatch):
        monkeypatch.setattr(settings, "CONTEXT_AUTOCOMPACT", True)
        monkeypatch.setattr(settings, "CONTEXT_COMPACT_LADDER", True)

        class _Scripted:
            def __init__(self):
                self.calls = 0

            async def compact(self, text: str, *, budget_tokens: int) -> str:
                self.calls += 1
                return "" if self.calls == 1 else text

        scripted = _Scripted()
        out = await maybe_compact([o.content for o in _traj()], scripted, 10_000)
        assert scripted.calls == 2
        assert out is not None

    @pytest.mark.asyncio
    async def test_engine_ladder_on_drop_loop_still_terminates(self, monkeypatch):
        monkeypatch.setattr(settings, "CONTEXT_AUTOCOMPACT", True)
        monkeypatch.setattr(settings, "CONTEXT_COMPACT_LADDER", True)

        class _IdentityOver:
            def __init__(self):
                self.calls = 0

            async def compact(self, text: str, *, budget_tokens: int) -> str:
                self.calls += 1
                return text + " tail" * 500  # 过五要素但超预算

        fake = _IdentityOver()
        eng = ContextEngine(compactor=fake)
        snap = await eng.build(_task("goal"), trajectory=_traj(5), budget_tokens=80)
        assert fake.calls == 4, "5 行轨迹 ⇒ 4→2→1→0 每轮都有压缩面"
        assert snap.truncated is True
        assert snap.token_estimate <= 80, "阶梯后仍超预算 ⇒ 丢弃循环确定性裁到预算内"

    @pytest.mark.asyncio
    async def test_engine_ladder_off_matches_no_compactor(self, monkeypatch):
        # ladder 默认关 + autocompact on：坏压缩器 ⇒ 与无压缩器控制组逐字节一致
        # （复核 Phase 22 TestL4On 契约不被阶梯分支破坏）
        monkeypatch.setattr(settings, "CONTEXT_AUTOCOMPACT", True)
        monkeypatch.setattr(settings, "CONTEXT_COMPACT_LADDER", False)
        bad = _LadderFake(["bland text without markers " * 20])
        with_c = ContextEngine(compactor=bad)
        without = ContextEngine()
        traj = _traj(5)
        s1 = await with_c.build(_task("goal"), trajectory=traj, budget_tokens=80)
        s2 = await without.build(_task("goal"), trajectory=traj, budget_tokens=80)
        assert bad.calls == 1
        assert s1.prompt == s2.prompt


# ---------------------------------------------------------------------------
# W3：主循环渐进重建
# ---------------------------------------------------------------------------


class _SpyContext:
    def __init__(self, fail_at: int | None = None) -> None:
        self.calls: list[tuple[int, int]] = []  # (trajectory_len, budget)
        self.fail_at = fail_at

    async def build(self, task, *, trajectory=(), budget_tokens=0):
        self.calls.append((len(trajectory), budget_tokens))
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise RuntimeError("rebuild down")
        parts = ["GOAL " + task.goal]
        parts.extend(f"[{o.step_id}] {o.content}" for o in trajectory)
        return ContextSnapshot(prompt="\n".join(parts), token_estimate=1)


class _TwoStepPlanner:
    async def plan(self, task, *, context=None):
        return Plan(
            steps=(
                PlanStep(id="a", action="act", tool="act"),
                PlanStep(id="b", action="act", tool="act"),
            )
        )


class _AlwaysPassVerifier:
    async def verify(self, task, plan, observations):
        from agentix.domain.agent.types import VerificationResult

        return VerificationResult(passed=True, reason="always")


@pytest.mark.asyncio
async def test_budget_zero_single_build_baseline_equal():
    ctx = _SpyContext()
    from agentix.orchestration.executor import MappingStepExecutor

    loop = AgentLoop(
        planner=_TwoStepPlanner(),
        context=ctx,
        verifier=_AlwaysPassVerifier(),
        executor=MappingStepExecutor(tools={"act": lambda **kw: "done"}),
        max_steps=10,
    )
    result = await loop.run(_task())
    assert result.terminal == "completed"
    assert ctx.calls == [(0, 0)], "默认 0 ⇒ 恰好一次构建，trajectory=()、budget=0"


@pytest.mark.asyncio
async def test_budget_on_rebuilds_per_step_with_growing_trajectory():
    ctx = _SpyContext()
    from agentix.orchestration.executor import MappingStepExecutor

    loop = AgentLoop(
        planner=_TwoStepPlanner(),
        context=ctx,
        verifier=_AlwaysPassVerifier(),
        executor=MappingStepExecutor(tools={"act": lambda **kw: "done"}),
        max_steps=10,
        context_budget_tokens=512,
    )
    result = await loop.run(_task())
    assert result.terminal == "completed"
    assert ctx.calls == [(0, 512), (1, 512)], (
        "budget>0 ⇒ 启动构建（无观测）+ 第二步执行前按累计观测重建 1 次"
    )


@pytest.mark.asyncio
async def test_budget_on_rebuild_failure_is_terminal_failed():
    ctx = _SpyContext(fail_at=2)  # 第二次构建（首步重建）失败
    from agentix.orchestration.executor import MappingStepExecutor

    loop = AgentLoop(
        planner=_TwoStepPlanner(),
        context=ctx,
        verifier=_AlwaysPassVerifier(),
        executor=MappingStepExecutor(tools={"act": lambda **kw: "done"}),
        max_steps=10,
        context_budget_tokens=256,
    )
    result = await loop.run(_task())
    assert result.terminal == "failed"
    assert result.reason.startswith("context_build_failed")
    assert result.steps_used == 1


@pytest.mark.asyncio
async def test_budget_on_rebuilt_context_is_trimmed():
    # 真实 SimpleContextBuilder：预算裁剪生效（token_estimate <= budget）
    builder = SimpleContextBuilder(rules=("RULE X",))
    from agentix.orchestration.executor import MappingStepExecutor

    loop = AgentLoop(
        planner=_TwoStepPlanner(),
        context=builder,
        verifier=_AlwaysPassVerifier(),
        executor=MappingStepExecutor(tools={"act": lambda **kw: "y" * 4000}),
        max_steps=10,
        context_budget_tokens=50,
    )
    result = await loop.run(_task("do things"))
    assert result.terminal == "completed"
    assert result.context.token_estimate <= 50, "重建后的上下文受预算裁剪"


@pytest.mark.asyncio
async def test_adapter_reads_setting_default_zero(monkeypatch):
    captured: dict = {}

    class _SpyLoop:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run(self, task):
            return LoopResult(
                task_id=task.task_id,
                final="ok",
                passed=True,
                plan=Plan(),
                context=ContextSnapshot(prompt=""),
            )

    monkeypatch.setattr(agent_loop_adapter, "AgentLoop", _SpyLoop)
    monkeypatch.setattr(settings, "AGENT_CONTEXT_BUDGET_TOKENS", 0)
    await agent_loop_adapter.run_agent_loop_ask("hi", "s-0")
    assert captured["context_budget_tokens"] == 0

    monkeypatch.setattr(settings, "AGENT_CONTEXT_BUDGET_TOKENS", 512)
    await agent_loop_adapter.run_agent_loop_ask("hi", "s-512")
    assert captured["context_budget_tokens"] == 512, "组合根把 settings 注入 AgentLoop"


# ---------------------------------------------------------------------------
# W5：诊断只读契约
# ---------------------------------------------------------------------------


class TestContextReport:
    def test_contract_and_shape(self):
        report = context_report()
        assert report["contract"] == {
            "read_only": True,
            "provider_calls": False,
            "mutates_state": False,
        }
        assert report["instructions"]["total_budget_chars"] == 12000
        assert report["instructions"]["max_chars_per_file"] == 4000
        assert report["instructions"]["project_files"] == ["AGENTS.md", "CLAUDE.md"]
        assert report["compaction"]["ladder_rounds"] == [4, 2, 1, 0]
        assert report["compaction"]["autocompact"] is False
        assert report["compaction"]["ladder"] is False
        assert report["loop"]["context_budget_tokens"] == 0
        assert "route" in report["status_bar_trusted_keys"]

    def test_pure_and_json_serializable(self, monkeypatch):
        monkeypatch.setattr(settings, "CONTEXT_COMPACT_LADDER", True)
        monkeypatch.setattr(settings, "AGENT_CONTEXT_BUDGET_TOKENS", 128)
        first = context_report()
        second = context_report()
        assert first == second, "纯函数：两次调用逐字节一致"
        assert json.loads(json.dumps(first)) == first, "JSON 可序列化（供 /ops/context）"
        assert first["compaction"]["ladder"] is True
        assert first["loop"]["context_budget_tokens"] == 128

    def test_does_not_touch_metrics_or_redis(self, monkeypatch):
        # 只读契约：不走 get_observability（其构造会连 Redis）
        def _boom():
            raise AssertionError("context_report must not touch observability/redis")

        import agentix.application.services.observability as obs_mod

        monkeypatch.setattr(obs_mod, "get_observability", _boom)
        report = context_report()
        assert report["contract"]["provider_calls"] is False

    def test_ops_route_registered(self):
        from agentix.interfaces.http.ops_router import router

        paths = {getattr(r, "path", "") for r in router.routes}
        assert "/ops/context" in paths


# ---------------------------------------------------------------------------
# 端到端：阶梯 + 逐步重建同框（flag 治理收口）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flags_off_full_stack_baseline(monkeypatch):
    """三键全默认（L4 关 / ladder 关 / budget 0）⇒ 行为与既有基线逐位一致。"""
    assert settings.CONTEXT_AUTOCOMPACT is False
    assert settings.CONTEXT_COMPACT_LADDER is False
    assert settings.AGENT_CONTEXT_BUDGET_TOKENS == 0
    ctx = _SpyContext()
    from agentix.orchestration.executor import MappingStepExecutor

    loop = AgentLoop(
        planner=_TwoStepPlanner(),
        context=ctx,
        verifier=_AlwaysPassVerifier(),
        executor=MappingStepExecutor(tools={"act": lambda **kw: "done"}),
        max_steps=10,
    )
    result = await loop.run(_task())
    assert result.terminal == "completed"
    assert ctx.calls == [(0, 0)]
    # 指令层默认调用：去重/预算均 no-op
    assert build_instructions(load_project=False, skills_enabled=False) == ()
