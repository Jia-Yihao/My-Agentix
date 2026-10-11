"""Phase 18 — 上下文分层：状态栏白名单 / 指令分层 / L1 Budget / L2 Snip /
L3 结果面 / 工具序稳定。

运行：./.venv/Scripts/python.exe -m pytest tests/test_agent_phase18_context_layering.py -q
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from agentix.agent.context import (
    PROJECT_INSTRUCTION_FILES,
    STATUS_BAR_TRUSTED_KEYS,
    ContextEngine,
    SimpleContextBuilder,
    build_instructions,
    find_project_instructions,
    load_project_instructions,
)
from agentix.orchestration.executor import MappingStepExecutor
from agentix.orchestration.strategies.multi_agent.manager import Manager, ManagerConfig, bound_result_face
from agentix.tools import list_openai_tools, registry as tool_registry
from agentix.tools.definitions import ToolSpec
from agentix.tools.registry_init import ensure_registered
from agentix.tools.selection import ToolSelectionPolicy, select_tool_schemas
from agentix.domain.agent.types import (
    ContextSnapshot,
    Observation,
    Plan,
    PlanStep,
    TaskSpec,
)


def _task(goal: str = "G") -> TaskSpec:
    return TaskSpec(goal=goal, task_id="t1")


def _step(sid: str = "s1", *, action: str = "act", tool: str | None = None) -> PlanStep:
    return PlanStep(id=sid, action=action, tool=tool)


def _plan(step: PlanStep) -> Plan:
    return Plan(steps=(step,))


_CTX = ContextSnapshot(prompt="")


# ---------------------------------------------------------------------------
# 状态栏白名单（不可模型自写）
# ---------------------------------------------------------------------------


class TestStatusBarWhitelist:
    @pytest.mark.asyncio
    async def test_simple_builder_drops_untrusted_keys(self):
        builder = SimpleContextBuilder(
            status_bar={"route": "chat", "model_said_status": "hacked", "tenant_id": "t1"}
        )
        snap = await builder.build(_task())
        assert snap.status_bar == {"route": "chat", "tenant_id": "t1"}
        assert "model_said_status" not in snap.status_bar
        assert "model_said_status" not in STATUS_BAR_TRUSTED_KEYS

    @pytest.mark.asyncio
    async def test_simple_builder_default_empty(self):
        snap = await SimpleContextBuilder().build(_task())
        assert snap.status_bar == {}

    @pytest.mark.asyncio
    async def test_builder_and_engine_share_trust_source(self):
        raw = {"route": "chat", "evil": "1"}
        b = SimpleContextBuilder(status_bar=dict(raw))
        e = ContextEngine(status_bar=dict(raw))
        sb = await b.build(_task())
        se = await e.build(_task())
        assert sb.status_bar == se.status_bar == {"route": "chat"}


# ---------------------------------------------------------------------------
# 指令分层（settings → 项目 → 会话）
# ---------------------------------------------------------------------------


class TestBuilderRules:
    @pytest.mark.asyncio
    async def test_rules_render_as_static_prefix(self):
        builder = SimpleContextBuilder(rules=("RULE-A", "RULE-B"))
        snap = await builder.build(_task("my goal"))
        assert snap.prompt.startswith("RULE-A\nRULE-B\n")
        assert snap.prompt.endswith("my goal")

    @pytest.mark.asyncio
    async def test_default_rules_prompt_byte_identical(self):
        builder = SimpleContextBuilder()
        snap = await builder.build(_task("my goal"))
        assert snap.prompt == "my goal"  # 零新键默认 = 与既有逐字节一致

    @pytest.mark.asyncio
    async def test_blank_rules_skipped(self):
        builder = SimpleContextBuilder(rules=("", "  ", "keep"))
        snap = await builder.build(_task("g"))
        assert snap.prompt == "keep\ng"

    @pytest.mark.asyncio
    async def test_rules_participate_in_budget_trim(self):
        builder = SimpleContextBuilder(rules=("R" * 50,))
        obs = [Observation(step_id="s1", content="x" * 100)]
        snap = await builder.build(_task("g"), trajectory=obs, budget_tokens=30)
        assert snap.truncated is True
        assert snap.prompt.startswith("R" * 50)  # 静态前缀永不被裁


class TestInstructionDiscovery:
    def test_priority_agents_over_claude(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("A", encoding="utf-8")
        (tmp_path / "CLAUDE.md").write_text("C", encoding="utf-8")
        found = find_project_instructions(tmp_path, stop=tmp_path)
        assert found is not None and found.name == "AGENTS.md"

    def test_walks_up_until_stop(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("# proj rules", encoding="utf-8")
        deep = tmp_path / "a" / "b"
        deep.mkdir(parents=True)
        found = find_project_instructions(deep, stop=tmp_path)
        assert found == tmp_path / "AGENTS.md"
        # stop 边界之外不看（hermetic：临时目录之上可能恰有约定文件）
        assert find_project_instructions(deep, stop=deep) is None

    def test_claude_md_only(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("C", encoding="utf-8")
        found = find_project_instructions(tmp_path, stop=tmp_path)
        assert found is not None and found.name == "CLAUDE.md"

    def test_absent_returns_empty(self, tmp_path):
        assert find_project_instructions(tmp_path, stop=tmp_path) is None
        assert load_project_instructions(tmp_path, stop=tmp_path) == ""

    def test_start_can_be_a_file(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("A", encoding="utf-8")
        start = tmp_path / "run.py"
        start.write_text("", encoding="utf-8")
        found = find_project_instructions(start, stop=tmp_path)
        assert found == tmp_path / "AGENTS.md"

    def test_load_truncates_deterministically(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("y" * 100, encoding="utf-8")
        out = load_project_instructions(tmp_path, max_chars=10, stop=tmp_path)
        assert out.startswith("y" * 10)
        assert "已截断" in out

    def test_load_utf8_chinese(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("# 规则\n- 中文内容", encoding="utf-8")
        assert "中文内容" in load_project_instructions(tmp_path, stop=tmp_path)

    def test_build_ordering_three_layers(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("PROJ", encoding="utf-8")
        blocks = build_instructions(
            settings_rules=("S1", ""),
            project_start=tmp_path,
            stop=tmp_path,
            session_rules=("SES",),
        )
        assert blocks == ("S1", "PROJ", "SES")

    def test_build_can_skip_project_layer(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("PROJ", encoding="utf-8")
        blocks = build_instructions(
            settings_rules=("S1",),
            project_start=tmp_path,
            stop=tmp_path,
            load_project=False,
            session_rules=("SES",),
        )
        assert blocks == ("S1", "SES")

    def test_build_skips_whitespace_project_file(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("  \n\t", encoding="utf-8")
        assert build_instructions(project_start=tmp_path, stop=tmp_path) == ()

    def test_convention_names_frozen(self):
        # 约定文件名是对外契约（零新键决策），变更须走文档审批
        assert PROJECT_INSTRUCTION_FILES == ("AGENTS.md", "CLAUDE.md")


# ---------------------------------------------------------------------------
# L1 Budget：MappingStepExecutor 出层限量
# ---------------------------------------------------------------------------


class TestExecutorLimiter:
    @pytest.mark.asyncio
    async def test_small_str_result_unchanged(self):
        ex = MappingStepExecutor(tools={"t": lambda **kw: "hello"})
        obs = await ex.execute(_task(), _plan(_step(tool="t")), _step(tool="t"), _CTX)
        assert obs.ok is True
        assert obs.content == "hello"
        assert obs.meta == {}  # 未触限 = 与既有行为逐字节一致

    @pytest.mark.asyncio
    async def test_small_dict_result_unchanged(self):
        ex = MappingStepExecutor(tools={"t": lambda **kw: {"b": 2, "a": 1}})
        obs = await ex.execute(_task(), _plan(_step(tool="t")), _step(tool="t"), _CTX)
        assert obs.content == "a=1\nb=2"
        assert obs.meta == {}

    @pytest.mark.asyncio
    async def test_long_str_truncated_with_notice(self):
        ex = MappingStepExecutor(tools={"t": lambda **kw: "y" * 9000})
        obs = await ex.execute(_task(), _plan(_step(tool="t")), _step(tool="t"), _CTX)
        assert obs.ok is True
        assert len(obs.content) < 9000
        assert "截断" in obs.content  # 不静默：模型看得见被截断
        assert obs.meta["limited"]["truncated"] is True
        assert obs.meta["limited"]["truncated_chars"] == 1000

    @pytest.mark.asyncio
    async def test_rows_paged_with_has_more(self):
        rows = [{"id": i} for i in range(150)]
        ex = MappingStepExecutor(tools={"t": lambda **kw: rows})
        obs = await ex.execute(_task(), _plan(_step(tool="t")), _step(tool="t"), _CTX)
        assert obs.ok is True
        assert obs.meta["limited"]["has_more"] is True
        assert obs.meta["limited"]["total_rows"] == 150
        assert "rows=" in obs.content
        assert "page=" in obs.content

    @pytest.mark.asyncio
    async def test_async_tool_limited_too(self):
        async def big(**kwargs):
            return "z" * 9000

        ex = MappingStepExecutor(tools={"t": big})
        obs = await ex.execute(_task(), _plan(_step(tool="t")), _step(tool="t"), _CTX)
        assert obs.ok is True
        assert obs.meta["limited"]["truncated"] is True
        assert "截断" in obs.content

    @pytest.mark.asyncio
    async def test_unregistered_tool_unchanged(self):
        ex = MappingStepExecutor(tools={})
        obs = await ex.execute(_task(), _plan(_step(tool="nope")), _step(tool="nope"), _CTX)
        assert obs.ok is False
        assert obs.error_code == "tool_not_found"


# ---------------------------------------------------------------------------
# L2 Snip：ContextEngine trim_policy
# ---------------------------------------------------------------------------


def _long_traj() -> list[Observation]:
    return [
        Observation(step_id=f"s{i}", content="x" * 100) for i in (1, 2, 3)
    ]


class TestSnipTrimPolicy:
    @pytest.mark.asyncio
    async def test_default_tail_keeps_oldest(self):
        engine = ContextEngine()
        snap = await engine.build(_task(), trajectory=_long_traj(), budget_tokens=60)
        assert snap.truncated is True
        assert "[s1|ok]" in snap.prompt
        assert "[s3|ok]" not in snap.prompt  # 从尾部（最新）丢弃

    @pytest.mark.asyncio
    async def test_head_drops_oldest_keeps_newest(self):
        engine = ContextEngine(trim_policy="head")
        snap = await engine.build(_task(), trajectory=_long_traj(), budget_tokens=60)
        assert snap.truncated is True
        assert "[s3|ok]" in snap.prompt
        assert "[s1|ok]" not in snap.prompt  # 按龄丢最旧行（Snip）

    @pytest.mark.asyncio
    async def test_head_never_drops_goal(self):
        engine = ContextEngine(trim_policy="head")
        snap = await engine.build(_task("GOAL-X"), trajectory=_long_traj(), budget_tokens=1)
        assert snap.truncated is True
        assert "GOAL-X" in snap.prompt
        assert "[s1|ok]" not in snap.prompt and "[s3|ok]" not in snap.prompt

    @pytest.mark.asyncio
    async def test_deterministic_same_input_same_prompt(self):
        for policy in ("tail", "head"):
            engine = ContextEngine(trim_policy=policy)
            a = await engine.build(_task(), trajectory=_long_traj(), budget_tokens=60)
            b = await engine.build(_task(), trajectory=_long_traj(), budget_tokens=60)
            assert a.prompt == b.prompt, policy

    def test_invalid_policy_rejected(self):
        with pytest.raises(ValueError, match="trim_policy"):
            ContextEngine(trim_policy="middle")


# ---------------------------------------------------------------------------
# L3 Isolation：Manager 结果面摘要协议
# ---------------------------------------------------------------------------


class TestResultFace:
    def test_passthrough_within_limit(self):
        assert bound_result_face("abc", max_chars=10) == "abc"
        assert bound_result_face("x" * 50, max_chars=50) == "x" * 50

    def test_truncated_with_explicit_notice(self):
        out = bound_result_face("x" * 500, max_chars=50)
        assert out.startswith("x" * 50)
        assert "结果面已截断" in out and "省略 450 字符" in out
        assert len(out) < 500

    def test_disabled_when_non_positive(self):
        assert bound_result_face("x" * 500, max_chars=0) == "x" * 500
        assert bound_result_face("x" * 500, max_chars=-1) == "x" * 500

    @pytest.mark.asyncio
    async def test_manager_bounds_peer_face(self):
        def proposer(**kwargs):
            return "accept " + "x" * 500

        def reviewer(**kwargs):
            return "approve"

        mgr = Manager(config=ManagerConfig(max_result_chars=50))
        out = await mgr.run("g", proposer=proposer, reviewer=reviewer)
        assert out.ok is True
        assert "结果面已截断" in out.final_output
        assert len(out.final_output) < 200
        assert "结果面已截断" in mgr.blackboard[0]

    @pytest.mark.asyncio
    async def test_manager_default_keeps_short_untouched(self):
        def proposer(**kwargs):
            return "accept short proposal"

        def reviewer(**kwargs):
            return "approve"

        mgr = Manager()
        out = await mgr.run("g", proposer=proposer, reviewer=reviewer)
        assert out.ok is True
        assert out.final_output == "accept short proposal"


# ---------------------------------------------------------------------------
# 稳定前缀：工具序稳定
# ---------------------------------------------------------------------------


class _ToolA(BaseModel):
    pass


class _ToolB(BaseModel):
    pass


class _ToolC(BaseModel):
    pass


class TestToolOrderStable:
    @staticmethod
    def _names(tools: list[dict]) -> list[str]:
        return [t["function"]["name"] for t in tools]

    def test_list_openai_tools_stable_across_calls(self):
        ensure_registered()
        first = self._names(list_openai_tools())
        second = self._names(list_openai_tools())
        assert first == second
        assert len(first) == len(set(first))  # 无重复
        assert first == list(tool_registry.tool_names())  # 注册序

    def test_select_preserves_relative_order(self):
        specs = [
            ToolSpec(name="_ToolA", description="a"),
            ToolSpec(name="_ToolB", description="b"),
            ToolSpec(name="_ToolC", description="c"),
        ]
        policy = ToolSelectionPolicy(denied_tools=frozenset({"_ToolB"}))
        kept = select_tool_schemas([_ToolA, _ToolB, _ToolC], policy, spec_overrides=specs)
        assert kept == [_ToolA, _ToolC]  # 过滤不打乱相对序

    def test_select_risk_filter_preserves_order(self):
        specs = [
            ToolSpec(name="_ToolA", description="a"),
            ToolSpec(name="_ToolB", description="b", risk_level="L2", control="人工审批"),
            ToolSpec(name="_ToolC", description="c"),
        ]
        policy = ToolSelectionPolicy(max_risk_level="L1")
        kept = select_tool_schemas([_ToolA, _ToolB, _ToolC], policy, spec_overrides=specs)
        assert kept == [_ToolA, _ToolC]
