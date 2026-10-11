"""A3（统一执行链·图内规划收敛）守卫测试。

两半：
1. **TEXT2SQL 死 flag 处置**——`TEXT2SQL_ENABLED` 从 Settings / selfcheck
   合法省略清单 / `.env.example` / `PROJECT_SPEC` 配置表全量清除；
2. **KG planner 影子对拍 → 切流**——`KG_PLANNER_SHADOW_ENABLED`（对拍）与
   `KG_PLANNER_UNIFIED_ENABLED`（切流）双 flag 默认关、flag-off 图内私有链
   逐位不变；影子报告只记不改、非驱动侧异常绝不影响输出；切流后统一
   Planner（零 LLM 单步）驱动分解、私有链零调用。

运行：./.venv/Scripts/python.exe -m pytest tests/test_p63_planner_convergence.py -q
"""

from pathlib import Path

import pytest
from langchain_core.runnables.base import RunnableLambda

from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.models import Task
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.planner import unified as planner_unified
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.planner.models import PlannerOutput
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.planner.node import (
    create_planner_node,
)
from agentix.config import settings as settings_instance

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakePlannerLLM:
    """``with_structured_output`` 返回固定 ``PlannerOutput``/固定异常的假 LLM。

    ``calls`` = 真实发生过的图内规划链调用次数（证明 flag-off 唯一调用方
    与切流后零调用）。
    """

    def __init__(self, response: PlannerOutput | None = None, exc: Exception | None = None):
        self.response = response
        self.exc = exc
        self.calls = 0

    def with_structured_output(self, schema):
        async def _respond(_inputs):
            self.calls += 1
            if self.exc is not None:
                raise self.exc
            return self.response

        return RunnableLambda(_respond)


def _tasks(*questions: str, parent: str = "Q") -> list[Task]:
    return [Task(question=q, parent_task=parent) for q in questions]


@pytest.fixture(autouse=True)
def _clean_reports():
    planner_unified.clear_reports()
    yield
    planner_unified.clear_reports()


@pytest.fixture()
def flags(monkeypatch):
    """把两个 A3 flag 都钉在默认关，测试内按需置开。"""
    monkeypatch.setattr(settings_instance, "KG_PLANNER_SHADOW_ENABLED", False)
    monkeypatch.setattr(settings_instance, "KG_PLANNER_UNIFIED_ENABLED", False)


class TestFlagDefaultsAndFiveSync:
    def test_both_flags_default_off(self, flags):
        assert settings_instance.KG_PLANNER_SHADOW_ENABLED is False
        assert settings_instance.KG_PLANNER_UNIFIED_ENABLED is False

    def test_text2sql_dead_flag_removed_from_settings(self):
        assert not hasattr(settings_instance, "TEXT2SQL_ENABLED")

    def test_text2sql_dead_flag_removed_from_config_surfaces(self):
        selfcheck_src = (PROJECT_ROOT / "agentix" / "config" / "selfcheck.py").read_text(encoding="utf-8")
        env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        project_spec = (PROJECT_ROOT / "docs" / "PROJECT_SPEC.md").read_text(encoding="utf-8")
        assert "TEXT2SQL_ENABLED" not in selfcheck_src
        assert "TEXT2SQL_ENABLED" not in env_example
        assert "TEXT2SQL_ENABLED" not in project_spec

    def test_live_text2sql_keys_untouched(self):
        # 死 flag 是 ENABLED，活键（后端选择/Wren）不得误伤
        assert settings_instance.TEXT2SQL_BACKEND == "mysql"
        from agentix.config.selfcheck import DEFAULT_IGNORED_MISSING

        assert "TEXT2SQL_BACKEND" in DEFAULT_IGNORED_MISSING

    def test_new_flags_five_place_sync(self):
        from agentix.config.selfcheck import DEFAULT_IGNORED_MISSING

        env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        config_md = (PROJECT_ROOT / "docs" / "architecture" / "configuration.md").read_text(encoding="utf-8")
        for key in ("KG_PLANNER_SHADOW_ENABLED", "KG_PLANNER_UNIFIED_ENABLED"):
            assert hasattr(settings_instance, key)
            assert key in DEFAULT_IGNORED_MISSING
            assert f"{key}=false" in env_example
            # configuration.md 分组表 + 详情表两处落表（p41 键抽取正则吃详情行首列）
            assert config_md.count(f"`{key}`") >= 2, f"{key} 未在 configuration.md 表+详情双落"


class TestFlagOffBaseline:
    @pytest.mark.asyncio
    async def test_in_graph_chain_drives_and_unified_untouched(self, flags, monkeypatch):
        """默认关：图内链唯一调用方，统一 Planner 零触达、零报告。"""
        llm = FakePlannerLLM(response=PlannerOutput(tasks=_tasks("a", "b", parent="Q")))
        spies: list[str] = []

        async def _spy(question: str):
            spies.append(question)
            return _tasks(question)

        monkeypatch.setattr(planner_unified, "unified_decompose", _spy)
        node = create_planner_node(llm=llm)

        result = await node({"question": "Q"})

        assert llm.calls == 1
        assert spies == []
        assert [t.question for t in result["tasks"]] == ["a", "b"]
        assert result["next_action"] == "tool_selection"
        assert planner_unified.get_reports() == ()

    @pytest.mark.asyncio
    async def test_empty_decomposition_falls_back_to_single_task(self, flags):
        llm = FakePlannerLLM(response=PlannerOutput(tasks=[]))
        node = create_planner_node(llm=llm)

        result = await node({"question": "Q"})

        assert [t.question for t in result["tasks"]] == ["Q"]
        assert result["tasks"][0].parent_task == "Q"

    @pytest.mark.asyncio
    async def test_ignore_node_skips_chain_even_if_unified_flag_on(self, flags, monkeypatch):
        """ignore_node 语义优先：任何 flag 下都不调 Planner，单任务兜底。"""
        monkeypatch.setattr(settings_instance, "KG_PLANNER_UNIFIED_ENABLED", True)
        llm = FakePlannerLLM()
        called: list[str] = []

        async def _spy(question: str):
            called.append(question)
            return _tasks(question)

        monkeypatch.setattr(planner_unified, "unified_decompose", _spy)
        node = create_planner_node(llm=llm, ignore_node=True)

        result = await node({"question": "Q"})

        assert llm.calls == 0
        assert called == []
        assert [t.question for t in result["tasks"]] == ["Q"]
        assert planner_unified.get_reports() == ()


class TestShadowCompare:
    @pytest.mark.asyncio
    async def test_in_graph_drives_output_recorded_only(self, flags, monkeypatch):
        """影子开：图内链驱动输出，统一侧并行对拍，输出不受影响。"""
        monkeypatch.setattr(settings_instance, "KG_PLANNER_SHADOW_ENABLED", True)
        llm = FakePlannerLLM(response=PlannerOutput(tasks=_tasks("a", "b", parent="Q")))

        async def _unified(question: str):
            return _tasks("a", "b", parent=question)

        monkeypatch.setattr(planner_unified, "unified_decompose", _unified)
        node = create_planner_node(llm=llm)

        result = await node({"question": "Q"})

        assert [t.question for t in result["tasks"]] == ["a", "b"]
        assert llm.calls == 1
        reports = planner_unified.get_reports()
        assert len(reports) == 1
        report = reports[0]
        assert report.driver == "in_graph"
        assert report.in_graph == ("a", "b")
        assert report.unified == ("a", "b")
        assert report.agree is True
        assert report.error is None

    @pytest.mark.asyncio
    async def test_mismatch_is_reported_not_corrected(self, flags, monkeypatch):
        monkeypatch.setattr(settings_instance, "KG_PLANNER_SHADOW_ENABLED", True)
        llm = FakePlannerLLM(response=PlannerOutput(tasks=_tasks("a", "b", parent="Q")))

        async def _unified(question: str):
            return _tasks(question)  # 单任务（单步规划口径）

        monkeypatch.setattr(planner_unified, "unified_decompose", _unified)
        node = create_planner_node(llm=llm)

        result = await node({"question": "Q"})

        # 不一致只进报告——输出仍是图内侧原样（对拍不纠偏）
        assert [t.question for t in result["tasks"]] == ["a", "b"]
        (report,) = planner_unified.get_reports()
        assert report.agree is False
        assert report.unified == ("Q",)

    @pytest.mark.asyncio
    async def test_shadow_side_error_never_breaks_output(self, flags, monkeypatch):
        monkeypatch.setattr(settings_instance, "KG_PLANNER_SHADOW_ENABLED", True)
        llm = FakePlannerLLM(response=PlannerOutput(tasks=_tasks("a", parent="Q")))

        async def _boom(question: str):
            raise RuntimeError("shadow planner down")

        monkeypatch.setattr(planner_unified, "unified_decompose", _boom)
        node = create_planner_node(llm=llm)

        result = await node({"question": "Q"})  # 不抛

        assert [t.question for t in result["tasks"]] == ["a"]
        (report,) = planner_unified.get_reports()
        assert report.error is not None
        assert "RuntimeError" in report.error
        assert report.agree is False
        assert report.unified == ()


class TestUnifiedCutover:
    @pytest.mark.asyncio
    async def test_unified_drives_and_in_graph_chain_never_runs(self, flags, monkeypatch):
        """切流：统一 Planner 驱动，图内私有链零 LLM 调用。"""
        monkeypatch.setattr(settings_instance, "KG_PLANNER_UNIFIED_ENABLED", True)
        llm = FakePlannerLLM(response=PlannerOutput(tasks=_tasks("never", parent="Q")))

        async def _unified(question: str):
            return _tasks("u1", "u2", parent=question)

        monkeypatch.setattr(planner_unified, "unified_decompose", _unified)
        node = create_planner_node(llm=llm)

        result = await node({"question": "Q"})

        assert llm.calls == 0  # 私有链不再执行
        assert [t.question for t in result["tasks"]] == ["u1", "u2"]
        assert planner_unified.get_reports() == ()  # 影子关 = 不落报告

    @pytest.mark.asyncio
    async def test_unified_empty_falls_back_like_in_graph(self, flags, monkeypatch):
        monkeypatch.setattr(settings_instance, "KG_PLANNER_UNIFIED_ENABLED", True)
        llm = FakePlannerLLM()

        async def _unified(question: str):
            return []

        monkeypatch.setattr(planner_unified, "unified_decompose", _unified)
        node = create_planner_node(llm=llm)

        result = await node({"question": "Q"})

        assert [t.question for t in result["tasks"]] == ["Q"]

    @pytest.mark.asyncio
    async def test_unified_driver_failure_propagates_honestly(self, flags, monkeypatch):
        """驱动侧异常按原语义直接抛（不静默降级），与图内链失败口径一致。"""
        monkeypatch.setattr(settings_instance, "KG_PLANNER_UNIFIED_ENABLED", True)
        llm = FakePlannerLLM()

        async def _boom(question: str):
            raise RuntimeError("unified planner down")

        monkeypatch.setattr(planner_unified, "unified_decompose", _boom)
        node = create_planner_node(llm=llm)

        with pytest.raises(RuntimeError, match="unified planner down"):
            await node({"question": "Q"})

    @pytest.mark.asyncio
    async def test_both_flags_unified_drives_with_reverse_shadow(self, flags, monkeypatch):
        """双开 = 切流驱动 + 图内链反向对拍；图内链异常只记报告。"""
        monkeypatch.setattr(settings_instance, "KG_PLANNER_UNIFIED_ENABLED", True)
        monkeypatch.setattr(settings_instance, "KG_PLANNER_SHADOW_ENABLED", True)
        llm = FakePlannerLLM(exc=RuntimeError("in-graph chain down"))

        async def _unified(question: str):
            return _tasks("u1", parent=question)

        monkeypatch.setattr(planner_unified, "unified_decompose", _unified)
        node = create_planner_node(llm=llm)

        result = await node({"question": "Q"})  # 图内链炸了也不影响输出

        assert [t.question for t in result["tasks"]] == ["u1"]
        assert llm.calls == 1  # 反向对拍确实跑了图内链
        (report,) = planner_unified.get_reports()
        assert report.driver == "unified"
        assert report.error is not None and "RuntimeError" in report.error
        assert report.in_graph == ()
        assert report.unified == ("u1",)
        assert report.agree is False

    @pytest.mark.asyncio
    async def test_custom_next_action_survives_cutover(self, flags, monkeypatch):
        monkeypatch.setattr(settings_instance, "KG_PLANNER_UNIFIED_ENABLED", True)
        llm = FakePlannerLLM()

        async def _unified(question: str):
            return _tasks("u", parent=question)

        monkeypatch.setattr(planner_unified, "unified_decompose", _unified)
        node = create_planner_node(llm=llm, next_action="tool_selection")

        result = await node({"question": "Q"})

        assert result["next_action"] == "tool_selection"


class TestReportStore:
    def test_record_builds_normalized_agree_report(self):
        planner_unified.clear_reports()
        report = planner_unified.record_report(
            question="Q",
            driver="in_graph",
            in_graph_tasks=[" a ", "b"],
            unified_tasks=["a", "b"],
        )
        assert report.seq == 1
        assert report.in_graph == ("a", "b")  # strip 归一
        assert report.agree is True
        assert report.error is None

    def test_error_forces_disagree(self):
        planner_unified.clear_reports()
        report = planner_unified.record_report(
            question="Q",
            driver="unified",
            in_graph_tasks=["a"],
            unified_tasks=["a"],
            error="ValueError: boom",
        )
        assert report.agree is False
        assert report.error == "ValueError: boom"

    def test_store_is_bounded_and_seq_monotonic(self):
        planner_unified.clear_reports()
        for i in range(planner_unified.MAX_REPORTS + 5):
            planner_unified.record_report(
                question=f"q{i}",
                driver="in_graph",
                in_graph_tasks=[],
                unified_tasks=[],
            )
        reports = planner_unified.get_reports()
        assert len(reports) == planner_unified.MAX_REPORTS
        # 有界淘汰最旧的，序号仍严格单调（最后一条 = 溢出前总数）
        assert reports[-1].seq == planner_unified.MAX_REPORTS + 5
        assert reports[0].seq == 6
        assert all(b.seq > a.seq for a, b in zip(reports, reports[1:]))

    def test_clear_resets_store_and_seq(self):
        planner_unified.record_report(
            question="Q", driver="in_graph", in_graph_tasks=[], unified_tasks=[]
        )
        planner_unified.clear_reports()
        assert planner_unified.get_reports() == ()
        report = planner_unified.record_report(
            question="Q", driver="in_graph", in_graph_tasks=[], unified_tasks=[]
        )
        assert report.seq == 1


class TestUnifiedDecompose:
    @pytest.mark.asyncio
    async def test_single_step_planner_maps_one_task_per_step(self):
        """现役统一 Planner = 单步零 LLM → 单任务（与图内空回退同构）。"""
        tasks = await planner_unified.unified_decompose("Q")
        assert [t.question for t in tasks] == ["Q"]
        assert tasks[0].parent_task == "Q"

    @pytest.mark.asyncio
    async def test_planner_seam_is_monkeypatchable(self, monkeypatch):
        """取用点可替换（未来 LLM 规划器换装口）。"""

        class _Multi:
            async def plan(self, task, *, context=None):
                from agentix.domain.agent.types import Plan, PlanStep

                return Plan(
                    steps=(PlanStep(id="s1", action="act"), PlanStep(id="s2", action="act")),
                    version=1,
                    planner="fake",
                )

        monkeypatch.setattr(planner_unified, "get_unified_planner", lambda: _Multi())
        tasks = await planner_unified.unified_decompose("Q")
        assert [t.question for t in tasks] == ["Q", "Q"]
