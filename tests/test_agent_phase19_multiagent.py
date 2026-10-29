"""Phase 19 multi_agent 接线验收（全离线，无 LLM / Redis / Neo4j / Docker）。

覆盖四件事：

1. **图选择** —— ``lg_builder.get_active_graph``：默认（flag 关）⇒ 扁平图
   零变化；``AGENTIX_ABLATION_MULTI_AGENT=true`` ⇒ supervisor 拓扑。
2. **supervisor 拓扑** —— 节点/边结构；``ROUTE_HANDLERS`` 与 ``route_query``
   的 Literal 收口逐位一致（路由 → 子 agent 映射不漂移）。
3. **supervisor_dispatch 行为** —— 结构审阅第 1 轮收敛（离线无 LLM 调用）、
   meta 携带（信息增量/失败模式）、Reviewer 否决与 Manager 异常的
   fail-open 回退、LLM 审阅否决 → 修订轮路径。
4. **指标流（规划 §7.6）** —— meta → ``run_eval`` 摘要聚合 → Benchmark 附录。
"""

import typing

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agentix.orchestration.langgraph import lg_builder, lg_supervisor
from agentix.orchestration.langgraph.lg_states import AgentState, Router
from agentix.config import ablation

AblationFlagNames = tuple(ablation.FLAG_DEFAULTS)


@pytest.fixture(autouse=True)
def _clean_ablation_env(monkeypatch):
    """每个用例从「无任何消融环境变量」的干净状态出发。"""
    for name in AblationFlagNames:
        monkeypatch.delenv(f"{ablation.ENV_PREFIX}{name.upper()}", raising=False)


@pytest.fixture(autouse=True)
def _offline_no_llm(monkeypatch):
    """默认离线：关闭 supervisor 的 LLM 审阅/修订（个别用例再显式打开）。"""
    monkeypatch.setattr(lg_supervisor, "_llm_available", lambda: False)


def _state(question: str = "违约金怎么算？", route_type: str = "general-query") -> AgentState:
    state = AgentState(messages=[HumanMessage(content=question)])
    state.router = Router(type=route_type, logic="test")
    return state


# ---------------------------------------------------------------------------
# 1. 图选择
# ---------------------------------------------------------------------------


class TestGraphSelection:
    def test_flag_off_returns_flat_graph(self):
        """默认 ⇒ 扁平图本身（flag-off 零变化的图选择语义）。"""
        assert lg_builder.get_active_graph() is lg_builder.graph

    def test_flag_on_returns_supervisor_graph(self, monkeypatch):
        monkeypatch.setenv(f"{ablation.ENV_PREFIX}MULTI_AGENT", "true")
        active = lg_builder.get_active_graph()
        assert active is not lg_builder.graph
        nodes = set(active.get_graph().nodes.keys())
        assert "supervisor_dispatch" in nodes
        # handler 不再是图节点（子 agent 由 ROUTE_HANDLERS 在节点内调用）
        assert "respond_to_general_query" not in nodes
        assert "create_kb_query" not in nodes
        # 入口链与扁平一致：窗口化 + 路由
        assert "message_window" in nodes
        assert "analyze_and_route_query" in nodes

    def test_flat_graph_has_no_supervisor_node(self):
        nodes = set(lg_builder.build_graph(checkpointer=None).get_graph().nodes.keys())
        assert "supervisor_dispatch" not in nodes
        for handler in lg_supervisor.ROUTE_HANDLERS:
            assert handler in nodes

    def test_chat_service_default_graph_getter_follows_flag(self, monkeypatch):
        from agentix.application.services.chat_service import ChatService

        svc = ChatService()
        assert svc._graph_getter() is lg_builder.get_active_graph()

        monkeypatch.setenv(f"{ablation.ENV_PREFIX}MULTI_AGENT", "true")
        nodes = set(svc._graph_getter().get_graph().nodes.keys())
        assert "supervisor_dispatch" in nodes


# ---------------------------------------------------------------------------
# 2. 拓扑结构 / 路由收口
# ---------------------------------------------------------------------------


class TestSupervisorTopology:
    def test_supervisor_graph_structure(self):
        g = lg_supervisor.build_supervisor_graph(checkpointer=None)
        nodes = set(g.get_graph().nodes.keys())
        assert {"message_window", "analyze_and_route_query", "supervisor_dispatch"} <= nodes

    def test_route_handlers_match_route_query_literal(self):
        """``ROUTE_HANDLERS`` 必须与 ``route_query`` 的 Literal 收口一致。"""
        # M3-3 起 lg_builder 的 `route_query` 是延迟解析壳、不携带 Literal 注解
        # （壳抄注解 = 第二份契约源）；绊线改读真源 router.route_query，
        # 判定强度不降（读的正是契约真源）。
        from agentix.application.agents import router as router_module

        ret = typing.get_type_hints(router_module.route_query)["return"]
        assert set(typing.get_args(ret)) == set(lg_supervisor.ROUTE_HANDLERS)


# ---------------------------------------------------------------------------
# 3. supervisor_dispatch 行为
# ---------------------------------------------------------------------------


class _CountingHandler:
    """充当子 agent：计数调用并返回固定草稿。"""

    def __init__(self, content: str = "这是子 agent 的草稿答案。", sources=None):
        self.calls = 0
        self.content = content
        self.sources = sources if sources is not None else [{"law": "民法典", "article": "585"}]

    async def __call__(self, state, *, config=None):
        self.calls += 1
        return {
            "messages": [AIMessage(content=self.content)],
            "sources": list(self.sources),
        }


class TestSupervisorDispatch:
    @pytest.mark.asyncio
    async def test_structural_converge_round1(self, monkeypatch):
        """离线结构路径：第 1 轮收敛，handler 只执行一次，meta/sources 保真。"""
        handler = _CountingHandler()
        monkeypatch.setitem(lg_supervisor.ROUTE_HANDLERS, "respond_to_general_query", handler)

        result = await lg_supervisor.supervisor_dispatch(_state(), config={})

        assert handler.calls == 1
        last = result["messages"][-1]
        assert last.content == "这是子 agent 的草稿答案。"
        assert result["sources"] == handler.sources
        meta = last.additional_kwargs["multi_agent"]
        assert meta["ok"] is True
        assert meta["rounds"] == 1
        assert meta["failure_mode"] == "none"
        assert meta["handler"] == "respond_to_general_query"
        assert 0 < meta["incremental_ratio"] <= 1.0

    @pytest.mark.asyncio
    async def test_router_type_maps_to_kb_handler(self, monkeypatch):
        """路由 kb-query ⇒ 走 create_kb_query 子 agent。"""
        handler = _CountingHandler(content="KB 草稿")
        monkeypatch.setitem(lg_supervisor.ROUTE_HANDLERS, "create_kb_query", handler)

        result = await lg_supervisor.supervisor_dispatch(
            _state(route_type="kb-query"), config={}
        )

        assert handler.calls == 1
        assert result["messages"][-1].content == "KB 草稿"
        assert result["messages"][-1].additional_kwargs["multi_agent"]["handler"] == "create_kb_query"

    @pytest.mark.asyncio
    async def test_empty_draft_rejected_and_falls_back(self, monkeypatch):
        """空答案 ⇒ 结构审阅否决、未收敛 ⇒ fail-open 回退草稿（meta 可观测）。"""
        handler = _CountingHandler(content="")
        monkeypatch.setitem(lg_supervisor.ROUTE_HANDLERS, "respond_to_general_query", handler)

        result = await lg_supervisor.supervisor_dispatch(_state(), config={})

        last = result["messages"][-1]
        assert last.content == ""
        meta = last.additional_kwargs["multi_agent"]
        assert meta["ok"] is False
        assert meta["failure_mode"] == "contradiction"

    @pytest.mark.asyncio
    async def test_manager_exception_falls_back_to_draft(self, monkeypatch):
        """Manager 抛异常 ⇒ 不挂链路，回退草稿并记 manager_exception。"""
        handler = _CountingHandler()
        monkeypatch.setitem(lg_supervisor.ROUTE_HANDLERS, "respond_to_general_query", handler)

        class _BoomManager:
            def __init__(self, **kwargs):
                pass

            async def run(self, goal, **kwargs):
                raise RuntimeError("boom")

        monkeypatch.setattr(lg_supervisor, "Manager", _BoomManager)

        result = await lg_supervisor.supervisor_dispatch(_state(), config={})

        last = result["messages"][-1]
        assert last.content == "这是子 agent 的草稿答案。"
        meta = last.additional_kwargs["multi_agent"]
        assert meta["ok"] is False
        assert meta["failure_mode"] == "manager_exception"

    @pytest.mark.asyncio
    async def test_llm_review_reject_then_revise_approve(self, monkeypatch):
        """LLM 可用：审阅否决 → 第 2 轮修订 → 复审通过（真实修订环）。"""
        from agentix.application.agents import llm_factory

        handler = _CountingHandler()
        monkeypatch.setitem(lg_supervisor.ROUTE_HANDLERS, "respond_to_general_query", handler)
        monkeypatch.setattr(lg_supervisor, "_llm_available", lambda: True)

        class _ReviewLLM:
            def __init__(self):
                self.system_prompts = []

            async def ainvoke(self, messages):
                system = messages[0]["content"]
                self.system_prompts.append(system)
                if system == lg_supervisor._REVISE_SYSTEM:
                    return AIMessage(content="修订后的答案（含正确引用）")
                if "修订后的答案" in messages[1]["content"]:
                    return AIMessage(content="approve: 已按意见修订")
                return AIMessage(content="reject: 引用不完整")

        fake_llm = _ReviewLLM()
        monkeypatch.setattr(llm_factory, "get_llm_client", lambda **kw: fake_llm)

        result = await lg_supervisor.supervisor_dispatch(_state(), config={})

        last = result["messages"][-1]
        assert last.content == "修订后的答案（含正确引用）"
        meta = last.additional_kwargs["multi_agent"]
        assert meta["ok"] is True
        assert meta["rounds"] == 2
        assert meta["failure_mode"] == "none"
        assert lg_supervisor._REVISE_SYSTEM in fake_llm.system_prompts

    @pytest.mark.asyncio
    async def test_llm_review_failure_fails_open_to_draft(self, monkeypatch):
        """审阅 LLM 抛异常 ⇒ fail-open approve（不挂链路、不改交付）。"""
        from agentix.application.agents import llm_factory

        handler = _CountingHandler()
        monkeypatch.setitem(lg_supervisor.ROUTE_HANDLERS, "respond_to_general_query", handler)
        monkeypatch.setattr(lg_supervisor, "_llm_available", lambda: True)

        class _BrokenLLM:
            async def ainvoke(self, messages):
                raise RuntimeError("llm down")

        monkeypatch.setattr(llm_factory, "get_llm_client", lambda **kw: _BrokenLLM())

        result = await lg_supervisor.supervisor_dispatch(_state(), config={})

        last = result["messages"][-1]
        assert last.content == "这是子 agent 的草稿答案。"
        assert last.additional_kwargs["multi_agent"]["ok"] is True


# ---------------------------------------------------------------------------
# 4. 指标流（run_eval 聚合 + Benchmark 附录）
# ---------------------------------------------------------------------------


class TestMetricsFlow:
    def test_pick_multi_agent_from_response(self):
        from evals.run_eval import _pick_multi_agent

        meta = {"ok": True, "rounds": 1, "failure_mode": "none", "incremental_ratio": 1.0}
        response = {
            "metadata": {
                "agent_state": {
                    "messages": [
                        AIMessage(content="x", additional_kwargs={"multi_agent": meta})
                    ]
                }
            }
        }
        assert _pick_multi_agent(response) == meta
        # 扁平拓扑：无 meta 键 ⇒ None
        assert _pick_multi_agent({"metadata": {"agent_state": {"messages": []}}}) is None
        assert _pick_multi_agent({}) is None

    def test_aggregate_multi_agent_summary(self):
        from evals.run_eval import _aggregate_multi_agent

        flat = _aggregate_multi_agent([{"multi_agent": None}, {"id": "x"}])
        assert flat == {"multi_agent_meta_count": 0}

        rows = [
            {"multi_agent": {"ok": True, "rounds": 1, "failure_mode": "none",
                             "incremental_ratio": 1.0}},
            {"multi_agent": {"ok": False, "rounds": 3, "failure_mode": "contradiction",
                             "incremental_ratio": 0.5}},
        ]
        agg = _aggregate_multi_agent(rows)
        assert agg["multi_agent_meta_count"] == 2
        assert agg["multi_agent_ok_rate"] == 0.5
        assert agg["multi_agent_rounds_mean"] == 2.0
        assert agg["multi_agent_incremental_ratio_mean"] == 0.75
        assert agg["multi_agent_failure_modes"] == {"none": 1, "contradiction": 1}

    def test_render_markdown_multi_agent_section(self):
        import evals.ablations.aggregate as agg_mod

        base = {key: None for key, _c, _f in agg_mod._METRIC_COLUMNS}
        with_ma = {
            **base,
            "id": "E_multi_agent",
            "label": "Multi-Agent",
            "total": 3,
            "multi_agent_meta_count": 3,
            "multi_agent_ok_rate": 1.0,
            "multi_agent_rounds_mean": 1.0,
            "multi_agent_incremental_ratio_mean": 1.0,
            "multi_agent_failure_modes": {"none": 3},
        }
        flat = {**base, "id": "D_tools", "label": "D", "total": 3,
                "multi_agent_meta_count": 0}
        text = agg_mod.render_markdown({"rows": [flat, with_ma], "missing": []})
        assert "Multi-Agent 运行指标" in text
        assert "失败模式: none=3" in text
        # 扁平变体不进附录
        flat_text = agg_mod.render_markdown({"rows": [flat], "missing": []})
        assert "Multi-Agent 运行指标" not in flat_text
