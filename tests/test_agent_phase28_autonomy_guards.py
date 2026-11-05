"""Phase 28 自主护栏验收（全离线，无 LLM / Redis / Neo4j / Docker）。

DoD 映射：

- flag-off：``respond_to_general_query`` / ``create_kb_query`` 入口行为与基线
  字节等价（仍走原路径、零 clarify 键）；状态栏无 ``max_iter``；
- flag-on：歧义输入（口语过短 / 低 confidence / 词表命中）→ 结构化澄清应答
  含候选项；明确输入 → 正常路由；
- 连续第二次澄清被强制路由（``clarify_armed`` 单轮约束，Ch5 禁再入循环）；
- 澄清应答不入 HITL（无 tool_calls）、不计失败（无 degraded）、不改路由终态；
- 状态栏 ``max_iter`` 仅 flag 开写入、为常量、白名单拒未知键；
- 剩余 ≤ 2 → ``budget_notice`` 进轨迹 append 区，静态前缀哈希不变；
- 耗尽行为与基线一致（``budget_exceeded`` 语义回归）。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agentix.agent.context import STATUS_BAR_TRUSTED_KEYS, ContextEngine, SimpleContextBuilder
from agentix.agent import AgentLoop
from agentix.orchestration.executor import MappingStepExecutor
from agentix.policies.planner import DeclaredStepsPlanner
from agentix.application.agents import clarify, general_handler
from agentix.orchestration.langgraph.lg_states import AgentState, Router
from agentix.application.services import agent_loop_adapter
from agentix.application.services.budget_notice import (
    budget_in_notice_zone,
    record_budget_notice,
)
from agentix.config import ablation
from agentix.config import settings as app_settings
from agentix.domain.agent.types import TaskSpec


# ---------------------------------------------------------------------------
# 夹具与假件
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_ablation_env(monkeypatch):
    """每个用例从「无任何消融环境变量」的干净状态出发（同 test_ablation 范式）。"""
    for name in ablation.FLAG_DEFAULTS:
        monkeypatch.delenv(f"{ablation.ENV_PREFIX}{name.upper()}", raising=False)


def _state(question: str = "民法典关于违约金是怎么规定的？", **kwargs) -> AgentState:
    return AgentState(messages=[HumanMessage(content=question)], **kwargs)


class _Model:
    """记录调用、返回固定答复的假 chat model。"""

    def __init__(self, content: str = "模型直接作答。"):
        self.calls = []
        self._content = content

    async def ainvoke(self, messages, config=None, **kwargs):
        self.calls.append(messages)
        return AIMessage(content=self._content)


class _Workflow:
    def __init__(self, response=None):
        self.invocations = []
        self._response = response or {"answer": "KB 回答", "sources": [{"id": 1}]}

    async def ainvoke(self, payload, config=None):
        self.invocations.append(payload)
        return self._response


class _SentinelGateway:
    def __init__(self, *args, **kwargs):
        pass


class _StubRecorder:
    def __init__(self):
        self.records = []

    def record(self, *, kind, name, status="ok", detail=None, **kwargs):
        self.records.append(
            {"kind": kind, "name": name, "status": status, "detail": detail or {}}
        )


def _patch_general_llm(monkeypatch, model=None):
    model = model or _Model()
    monkeypatch.setattr(general_handler, "get_llm_client", lambda **kw: model)
    return model


def _patch_kb_pipeline(monkeypatch, workflow=None):
    from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent import (
        multi_tool,
    )

    workflow = workflow or _Workflow()
    monkeypatch.setattr(multi_tool, "create_kb_multi_tool_workflow", lambda **kw: workflow)
    monkeypatch.setattr(general_handler, "KnowledgeGateway", _SentinelGateway)
    monkeypatch.setattr(general_handler, "get_llm_client", lambda **kw: _Model())
    return workflow


def _patch_recorder(monkeypatch):
    rec = _StubRecorder()
    monkeypatch.setattr(
        "agentix.application.services.trajectory.get_trajectory_recorder",
        lambda: rec,
    )
    return rec


class _AlwaysPassVerifier:
    async def verify(self, task, plan, observations):
        from agentix.domain.agent.types import VerificationResult

        return VerificationResult(passed=True, reason="always")


def _steps_task(n: int) -> TaskSpec:
    return TaskSpec(
        goal="loop",
        task_id=f"p28-{n}",
        constraints={
            "steps": [
                {"id": f"s{i}", "action": "act", "tool": "act"} for i in range(n)
            ]
        },
    )


def _tools():
    return {"act": lambda **kw: "done"}


# ---------------------------------------------------------------------------
# 1. should_clarify 纯函数（词表 + confidence）
# ---------------------------------------------------------------------------


class TestShouldClarify:
    def test_too_short_triggers_first(self):
        d = clarify.should_clarify("离婚吗")
        assert d.should is True
        assert d.reason == "too_short"
        assert d.options, "澄清应答必须带候选项"

    def test_domain_conflict_hits_two_groups(self):
        d = clarify.should_clarify("盗窃行为属于刑事还是合同违约？")
        assert d.should is True
        assert d.reason == "domain_conflict"
        assert "刑事" in d.options and "合同/债务" in d.options

    def test_missing_subject_deictic_without_entity(self):
        d = clarify.should_clarify("这个应该怎么处理")
        assert d.should is True
        assert d.reason == "missing_subject"

    def test_low_confidence_signal(self):
        q = "民法典关于违约金是怎么规定的？"
        assert clarify.should_clarify(q, confidence=0.3).reason == "low_confidence"
        assert clarify.should_clarify(q, confidence=0.9).should is False
        assert clarify.should_clarify(q, confidence=None).should is False

    def test_clear_input_no_clarify(self):
        d = clarify.should_clarify("公司与员工的劳动合同违约金如何计算？", confidence=0.9)
        assert d.should is False
        assert d.reason == "" and d.options == ()

    def test_reply_is_structured_without_hitl_or_degraded(self):
        d = clarify.should_clarify("离婚吗")
        msg = clarify.clarify_reply(d)
        assert isinstance(msg, AIMessage)
        assert msg.additional_kwargs["clarify"]["reason"] == "too_short"
        assert msg.additional_kwargs["clarify"]["options"] == list(d.options)
        assert all(opt in msg.content for opt in d.options)
        # 不入 HITL（无 tool_calls）、不计失败（无 degraded/hitl 标记）
        assert not msg.tool_calls
        assert "degraded" not in msg.additional_kwargs
        assert "hitl" not in msg.additional_kwargs
        assert "human_review" not in msg.additional_kwargs


# ---------------------------------------------------------------------------
# 2. flag-off：入口行为与基线字节等价
# ---------------------------------------------------------------------------


class TestFlagOffEquivalence:
    @pytest.mark.asyncio
    async def test_respond_ambiguous_still_goes_to_llm(self, monkeypatch):
        monkeypatch.setattr(app_settings, "CLARIFY_ROUTING_ENABLED", False)
        model = _patch_general_llm(monkeypatch)

        result = await general_handler.respond_to_general_query(
            _state("离婚吗"), config={}
        )

        assert len(model.calls) == 1, "flag-off 必须照常走 LLM（字节等价）"
        assert result["messages"][0].content == "模型直接作答。"
        assert "clarify_armed" not in result, "flag-off 不得写状态通道"
        assert "clarify" not in result["messages"][0].additional_kwargs

    @pytest.mark.asyncio
    async def test_kb_ambiguous_still_invokes_workflow(self, monkeypatch):
        monkeypatch.setattr(app_settings, "CLARIFY_ROUTING_ENABLED", False)
        workflow = _patch_kb_pipeline(monkeypatch)

        result = await general_handler.create_kb_query(_state("离婚吗"), config={})

        assert len(workflow.invocations) == 1, "flag-off 必须照常走 KB workflow"
        assert "clarify_armed" not in result
        assert "clarify" not in result["messages"][0].additional_kwargs


# ---------------------------------------------------------------------------
# 3. flag-on：歧义 → 澄清应答；明确 → 正常；单轮约束
# ---------------------------------------------------------------------------


class TestClarifyFlagOn:
    @pytest.mark.asyncio
    async def test_short_input_returns_clarify_without_llm(self, monkeypatch):
        monkeypatch.setattr(app_settings, "CLARIFY_ROUTING_ENABLED", True)
        model = _patch_general_llm(monkeypatch)
        state = _state("离婚吗")

        result = await general_handler.respond_to_general_query(state, config={})

        assert model.calls == [], "澄清应答零 LLM 调用"
        assert result["clarify_armed"] is True, "单轮上膛"
        msg = result["messages"][0]
        assert msg.additional_kwargs["clarify"]["reason"] == "too_short"
        assert not msg.tool_calls and "degraded" not in msg.additional_kwargs
        # 不改路由终态：router 原样
        assert state.router.type == "general-query"

    @pytest.mark.asyncio
    async def test_low_confidence_input_clarifies(self, monkeypatch):
        monkeypatch.setattr(app_settings, "CLARIFY_ROUTING_ENABLED", True)
        model = _patch_general_llm(monkeypatch)
        state = _state(
            "民法典关于违约金是怎么规定的？",
            router=Router(type="general-query", confidence=0.2),
        )

        result = await general_handler.respond_to_general_query(state, config={})

        assert model.calls == []
        assert result["messages"][0].additional_kwargs["clarify"]["reason"] == "low_confidence"

    @pytest.mark.asyncio
    async def test_clear_input_routes_normally_no_state_write(self, monkeypatch):
        monkeypatch.setattr(app_settings, "CLARIFY_ROUTING_ENABLED", True)
        model = _patch_general_llm(monkeypatch)

        result = await general_handler.respond_to_general_query(
            _state("公司与员工的劳动合同违约金如何计算？"), config={}
        )

        assert len(model.calls) == 1
        assert "clarify_armed" not in result, "判定明确且未上膛 ⇒ 零状态写入"

    @pytest.mark.asyncio
    async def test_second_ambiguous_turn_is_forced_to_route(self, monkeypatch):
        """单轮约束：已上膛 ⇒ 连续第二次澄清被压制（强制走原路径）。"""
        monkeypatch.setattr(app_settings, "CLARIFY_ROUTING_ENABLED", True)
        model = _patch_general_llm(monkeypatch)

        result = await general_handler.respond_to_general_query(
            _state("离婚吗", clarify_armed=True), config={}
        )

        assert len(model.calls) == 1, "第二轮必须给出路由结果，不得再澄清"
        assert "clarify" not in result["messages"][0].additional_kwargs
        assert "clarify_armed" not in result, "压制期间既不上膛也不解膛"

    @pytest.mark.asyncio
    async def test_clear_turn_disarms_to_rearmable(self, monkeypatch):
        """上膛后遇到明确输入 ⇒ 正常作答并回写 clarify_armed=False（恢复可澄清）。"""
        monkeypatch.setattr(app_settings, "CLARIFY_ROUTING_ENABLED", True)
        model = _patch_general_llm(monkeypatch)

        result = await general_handler.respond_to_general_query(
            _state("公司与员工的劳动合同违约金如何计算？", clarify_armed=True),
            config={},
        )

        assert len(model.calls) == 1
        assert result["clarify_armed"] is False

    @pytest.mark.asyncio
    async def test_kb_entry_clarifies_before_workflow(self, monkeypatch):
        monkeypatch.setattr(app_settings, "CLARIFY_ROUTING_ENABLED", True)
        workflow = _patch_kb_pipeline(monkeypatch)

        result = await general_handler.create_kb_query(_state("离婚吗"), config={})

        assert workflow.invocations == [], "澄清在 workflow 之前返回"
        assert result["clarify_armed"] is True
        assert result["messages"][0].additional_kwargs["clarify"]["reason"] == "too_short"

    @pytest.mark.asyncio
    async def test_kb_flag_off_vs_flag_on_byte_shape(self, monkeypatch):
        """flag-off 与 flag-on（明确输入）的返回形状一致（仅 flag-off 恒无键）。"""
        monkeypatch.setattr(app_settings, "CLARIFY_ROUTING_ENABLED", False)
        workflow = _patch_kb_pipeline(monkeypatch)
        off = await general_handler.create_kb_query(
            _state("公司与员工的劳动合同违约金如何计算？"), config={}
        )
        monkeypatch.setattr(app_settings, "CLARIFY_ROUTING_ENABLED", True)
        on = await general_handler.create_kb_query(
            _state("公司与员工的劳动合同违约金如何计算？"), config={}
        )
        assert len(workflow.invocations) == 2
        assert set(off.keys()) == set(on.keys()) == {"messages", "sources"}


# ---------------------------------------------------------------------------
# 4. 状态栏 max_iter（白名单 + 构造侧 flag-gated + 前缀稳定）
# ---------------------------------------------------------------------------


class TestStatusBarMaxIter:
    def test_whitelist_contains_max_iter(self):
        assert "max_iter" in STATUS_BAR_TRUSTED_KEYS

    @pytest.mark.asyncio
    async def test_engine_accepts_max_iter_rejects_unknown(self):
        e = ContextEngine(status_bar={"max_iter": "10", "model_said": "hacked"})
        snap = await e.build(TaskSpec(goal="g", task_id="x"))
        assert snap.status_bar == {"max_iter": "10"}
        assert "model_said" not in snap.prompt
        assert "STATUS max_iter=10" in snap.prompt

    @pytest.mark.asyncio
    async def test_same_config_prefix_hash_stable(self):
        async def prefix():
            e = ContextEngine(status_bar={"max_iter": "10"})
            return (await e.build(TaskSpec(goal="g", task_id="x"))).static_prefix

        p1, p2 = await prefix(), await prefix()
        assert hash(p1) == hash(p2), "同配置两次前缀哈希必须一致（常量键）"

    @pytest.mark.asyncio
    async def test_simple_builder_engine_share_max_iter(self):
        raw = {"max_iter": "10", "evil": "1"}
        sb = await SimpleContextBuilder(status_bar=dict(raw)).build(
            TaskSpec(goal="g", task_id="y")
        )
        se = await ContextEngine(status_bar=dict(raw)).build(TaskSpec(goal="g", task_id="y"))
        assert sb.status_bar == se.status_bar == {"max_iter": "10"}

    def test_construction_side_flag_gated(self, monkeypatch):
        monkeypatch.setattr(app_settings, "BUDGET_NOTICE_ENABLED", False)
        assert agent_loop_adapter._budget_status_bar() is None, "flag-off 无 max_iter"
        monkeypatch.setattr(app_settings, "BUDGET_NOTICE_ENABLED", True)
        assert agent_loop_adapter._budget_status_bar() == {
            "max_iter": str(int(app_settings.MAX_ITERATIONS))
        }


# ---------------------------------------------------------------------------
# 5. budget_notice（轨迹 append；前缀不变；耗尽语义回归）
# ---------------------------------------------------------------------------


class TestBudgetNotice:
    def test_notice_zone_boundaries(self):
        assert budget_in_notice_zone(step=8, limit=10) is True
        assert budget_in_notice_zone(step=7, limit=10) is False
        assert budget_in_notice_zone(step=10, limit=10) is True
        assert budget_in_notice_zone(step=0, limit=0) is False

    def test_flag_off_zero_write(self, monkeypatch):
        rec = _patch_recorder(monkeypatch)
        monkeypatch.setattr(app_settings, "BUDGET_NOTICE_ENABLED", False)
        assert record_budget_notice(steps_used=9, max_steps=10) is False
        assert rec.records == []

    def test_flag_on_in_zone_appends_once(self, monkeypatch):
        rec = _patch_recorder(monkeypatch)
        monkeypatch.setattr(app_settings, "BUDGET_NOTICE_ENABLED", True)
        assert record_budget_notice(steps_used=5, max_steps=10) is False, "区外零写入"
        assert rec.records == []
        assert record_budget_notice(steps_used=8, max_steps=10) is True
        assert len(rec.records) == 1
        r = rec.records[0]
        assert r["kind"] == "notice" and r["name"] == "budget_notice"
        assert r["detail"] == {"steps_used": 8, "max_steps": 10, "remaining": 2}

    @pytest.mark.asyncio
    async def test_budget_notice_does_not_touch_static_prefix(self, monkeypatch):
        rec = _patch_recorder(monkeypatch)
        monkeypatch.setattr(app_settings, "BUDGET_NOTICE_ENABLED", True)
        e = ContextEngine(status_bar={"max_iter": "3"})
        task = TaskSpec(goal="g", task_id="p28")
        before = (await e.build(task)).static_prefix
        assert record_budget_notice(steps_used=2, max_steps=3) is True
        after = (await e.build(task)).static_prefix
        assert before == after, "notice 只进轨迹 append 区，静态前缀哈希不变"
        assert rec.records[0]["name"] == "budget_notice"

    @pytest.mark.asyncio
    async def test_agent_loop_observer_fires_in_zone_only(self):
        calls = []
        loop = AgentLoop(
            planner=DeclaredStepsPlanner(),
            context=SimpleContextBuilder(),
            verifier=_AlwaysPassVerifier(),
            executor=MappingStepExecutor(_tools()),
            max_steps=3,
            budget_observer=lambda mx, su: calls.append((mx, su)),
        )
        result = await loop.run(_steps_task(3))
        assert result.terminal == "budget_exceeded", "3 步计划撞 3 步预算"
        assert calls == [(3, 1), (3, 2)], "仅剩余 ≤2 步时回调（steps_used=0 时剩余 3）"

    @pytest.mark.asyncio
    async def test_agent_loop_without_observer_unchanged(self):
        loop = AgentLoop(
            planner=DeclaredStepsPlanner(),
            context=SimpleContextBuilder(),
            verifier=_AlwaysPassVerifier(),
            executor=MappingStepExecutor(_tools()),
            max_steps=5,
        )
        result = await loop.run(_steps_task(2))
        assert result.terminal == "completed" and result.steps_used == 2

    @pytest.mark.asyncio
    async def test_budget_exceeded_semantics_unchanged_with_observer(self, monkeypatch):
        """耗尽行为与基线一致：注入 observer 不改 budget_exceeded 终态语义。"""
        calls = []
        loop = AgentLoop(
            planner=DeclaredStepsPlanner(),
            context=SimpleContextBuilder(),
            verifier=_AlwaysPassVerifier(),
            executor=MappingStepExecutor(_tools()),
            max_steps=2,
            budget_observer=lambda mx, su: calls.append((mx, su)),
        )
        result = await loop.run(_steps_task(3))
        assert result.terminal == "budget_exceeded"
        assert result.passed is False
        assert result.steps_used == 2
        assert result.reason == "budget_exceeded steps=2"
        assert calls == [(2, 0), (2, 1)]

    @pytest.mark.asyncio
    async def test_observer_exception_never_breaks_loop(self):
        def boom(mx, su):
            raise RuntimeError("observer down")

        loop = AgentLoop(
            planner=DeclaredStepsPlanner(),
            context=SimpleContextBuilder(),
            verifier=_AlwaysPassVerifier(),
            executor=MappingStepExecutor(_tools()),
            max_steps=2,
            budget_observer=boom,
        )
        result = await loop.run(_steps_task(3))
        assert result.terminal == "budget_exceeded"
