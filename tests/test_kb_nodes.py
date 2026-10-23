"""P1-1 拆分验收测试：kb_nodes/ 5 个节点独立单测（全部 mock 外部依赖）。

方案验收标准：
1. 5 个节点各有独立单测，全部 mock 外部依赖（LLM / KnowledgeService / HTTP）
2. 格式化纯函数零依赖可测
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from langchain_core.runnables import RunnableLambda

from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_formatters import (
    collect_sources,
    format_combined_local_results,
    format_external_results,
    format_milvus_results,
    format_postgres_results,
    format_results,
    history_to_text,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_nodes import (
    create_external_search_node,
    create_finalize_node,
    create_guardrails_node,
    create_local_search_node,
    create_router_node,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_states import (
    KBGuardrailsDecision,
    KBRouteDecision,
    KBWorkflowConfig,
)


def _make_llm(structured_result=None, final_content="合成回答"):
    """构造 mock LLM：with_structured_output 返回固定决策，ainvoke 返回固定文本。

    注意：prompt | structured_chain 会被 langchain 包装为 RunnableLambda，
    即以同步调用方式调用返回对象本身——所以 structured 必须是
    "可调用且返回 coroutine" 的 AsyncMock，而非仅带 ainvoke 属性的 MagicMock。
    """
    llm = MagicMock()
    # 真实 RunnableLambda 包装：prompt | chain 走 langchain 原生管道，稳定可 await
    structured = RunnableLambda(lambda _input: structured_result)
    llm.with_structured_output = MagicMock(return_value=structured)

    final_response = MagicMock()
    final_response.content = final_content
    llm.ainvoke = AsyncMock(return_value=final_response)
    return llm


def _make_cfg(**overrides):
    base = dict(
        top_k=3,
        similarity_threshold=0.5,
        postgres_search_url=None,
        external_url=None,
        allow_external_search=False,
        external_is_postgres=False,
        request_timeout=5.0,
        filter_expr=None,
        scope_text="测试范围",
    )
    base.update(overrides)
    return KBWorkflowConfig(**base)


def _run(coro):
    # 自建 loop：不依赖全局事件循环状态——pytest-asyncio 的 scoped policy 在
    # 部分测试顺序下会以 set_event_loop(None) 收尾（实测 p47 之后），复用全局
    # 状态会让本文件的同步用例随执行顺序时红时绿（get_event_loop 直接 RuntimeError）。
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# guardrails 节点
# ---------------------------------------------------------------------------

class TestGuardrailsNode:
    def test_proceed_when_in_scope(self):
        llm = _make_llm(KBGuardrailsDecision(decision="proceed", summary=None))
        node = create_guardrails_node(llm, scope_text="测试范围")
        result = _run(node({"question": "善意取得是什么？"}))
        assert result["guardrails_decision"] == "proceed"
        assert result["summary"] == ""
        assert result["steps"] == ["guardrails"]

    def test_end_when_out_of_scope(self):
        llm = _make_llm(KBGuardrailsDecision(decision="end", summary="越界"))
        node = create_guardrails_node(llm, scope_text="测试范围")
        result = _run(node({"question": "帮我写个段子"}))
        assert result["guardrails_decision"] == "end"
        assert result["summary"] == "越界"

    def test_default_summary_when_end_without_summary(self):
        llm = _make_llm(KBGuardrailsDecision(decision="end", summary=None))
        node = create_guardrails_node(llm, scope_text="测试范围")
        result = _run(node({"question": "越界问题"}))
        assert "不在测试数据集知识库的支持范围内" in result["summary"]


# ---------------------------------------------------------------------------
# router 节点
# ---------------------------------------------------------------------------

class TestRouterNode:
    def test_local_route_keeps_tools(self):
        llm = _make_llm(KBRouteDecision(route="local", rationale="r", tools=["postgres"]))
        node = create_router_node(llm, allow_external_search=True)
        result = _run(node({"question": "q", "history": []}))
        assert result["route"] == "local"
        assert result["kb_tools"] == ["postgres"]

    def test_external_downgraded_when_disabled(self):
        llm = _make_llm(KBRouteDecision(route="external", rationale="r", tools=["milvus"]))
        node = create_router_node(llm, allow_external_search=False)
        result = _run(node({"question": "q", "history": []}))
        assert result["route"] == "local"

    def test_default_tools_when_empty(self):
        llm = _make_llm(KBRouteDecision(route="local", rationale="r", tools=[]))
        node = create_router_node(llm, allow_external_search=False)
        result = _run(node({"question": "q", "history": []}))
        assert result["kb_tools"] == ["postgres", "milvus"]

    def test_both_tools_pass_through(self):
        # tools 域由 KBRouteDecision 的 Literal 保证，只能为 milvus/postgres；
        # 此用例验证合法组合原样透传
        llm = _make_llm(KBRouteDecision(route="local", rationale="r", tools=["postgres", "milvus"]))
        node = create_router_node(llm, allow_external_search=False)
        result = _run(node({"question": "q", "history": []}))
        assert result["kb_tools"] == ["postgres", "milvus"]


# ---------------------------------------------------------------------------
# local_search 节点
# ---------------------------------------------------------------------------

class TestLocalSearchNode:
    def test_empty_question_short_circuit(self):
        node = create_local_search_node(MagicMock(), _make_cfg())
        result = _run(node({"question": "  "}))
        assert result["milvus_results"] == []
        assert result["local_results"] == []

    def test_milvus_fallback_when_postgres_unconfigured(self):
        ks = MagicMock()
        ks.reranker.enabled = False
        ks.search = AsyncMock(return_value=[{"content": "法条A", "metadata": {}}])
        node = create_local_search_node(ks, _make_cfg())
        result = _run(node({"question": "q", "kb_tools": ["postgres", "milvus"]}))
        # postgres_search_url 未配置 → 直接 milvus 兜底
        ks.search.assert_awaited_once()
        assert result["postgres_results"] == []
        assert result["local_results"][0]["tool"] == "milvus"

    def test_postgres_selected_but_no_url_falls_to_milvus(self):
        ks = MagicMock()
        ks.reranker.enabled = False
        ks.search = AsyncMock(return_value=[])
        cfg = _make_cfg(postgres_search_url=None)
        node = create_local_search_node(ks, cfg)
        result = _run(node({"question": "q", "kb_tools": ["postgres"]}))
        # milvus 也无结果 → 空 local_results
        assert result["local_results"] == []

    def test_external_fallback_when_local_empty(self):
        ks = MagicMock()
        ks.reranker.enabled = False
        ks.search = AsyncMock(return_value=[])
        cfg = _make_cfg(allow_external_search=True, external_url="http://ext/search")
        node = create_local_search_node(ks, cfg)
        result = _run(node({"question": "q", "kb_tools": ["milvus"], "route": "local"}))
        assert result["route"] == "external"


# ---------------------------------------------------------------------------
# external_search 节点
# ---------------------------------------------------------------------------

class TestExternalSearchNode:
    def test_disabled_returns_empty(self):
        node = create_external_search_node(_make_cfg(allow_external_search=False))
        result = _run(node({"question": "q"}))
        assert result["external_results"] == []

    def test_same_source_postgres_skip(self):
        cfg = _make_cfg(
            allow_external_search=True,
            external_url="http://pg/api/v1/knowledge/search",
            external_is_postgres=True,
        )
        node = create_external_search_node(cfg)
        result = _run(node({"question": "q", "kb_tools": ["postgres"]}))
        assert result["external_results"] == []


# ---------------------------------------------------------------------------
# finalize 节点
# ---------------------------------------------------------------------------

class TestFinalizeNode:
    # P1-4：finalize 声明了 ``config``（langgraph 按签名注入），并转发给 LLM
    # 调用以便 token 冒泡；直接单测需显式传入一个最小 config。
    _CFG = {"configurable": {}}

    def test_guardrails_end_short_circuit(self):
        llm = _make_llm()
        node = create_finalize_node(llm, top_k=3)
        result = _run(node({"guardrails_decision": "end", "summary": "不可回答"}, config=self._CFG))
        assert result["answer"] == "不可回答"
        assert result["sources"] == []
        llm.ainvoke.assert_not_awaited()

    def test_no_results_fallback_answer(self):
        llm = _make_llm()
        node = create_finalize_node(llm, top_k=3)
        result = _run(node({"guardrails_decision": "proceed", "question": "q"}, config=self._CFG))
        assert "暂未找到相关记载" in result["answer"]
        llm.ainvoke.assert_not_awaited()

    def test_synthesises_answer_with_sources(self):
        llm = _make_llm(final_content="依据《民法典》第一条……")
        node = create_finalize_node(llm, top_k=3)
        state = {
            "guardrails_decision": "proceed",
            "question": "q",
            "milvus_results": [{"content": "长文本释义", "tool": "milvus", "metadata": {"title": "民法典释义"}}],
            "postgres_results": [],
            "local_results": [{"content": "长文本释义", "tool": "milvus", "metadata": {"title": "民法典释义"}}],
            "external_results": [],
        }
        result = _run(node(state, config=self._CFG))
        assert result["answer"] == "依据《民法典》第一条……"
        assert result["sources"] == ["民法典释义"]
        llm.ainvoke.assert_awaited_once()

    def test_llm_failure_degrades_to_context(self):
        llm = _make_llm()
        llm.ainvoke = AsyncMock(side_effect=RuntimeError("llm down"))
        node = create_finalize_node(llm, top_k=3)
        state = {
            "guardrails_decision": "proceed",
            "question": "q",
            "milvus_results": [],
            "postgres_results": [{"content": "结构化结果", "tool": "postgres", "metadata": {}}],
            "local_results": [{"content": "结构化结果", "tool": "postgres", "metadata": {}}],
            "external_results": [],
        }
        result = _run(node(state, config=self._CFG))
        assert "结构化结果" in result["answer"]


# ---------------------------------------------------------------------------
# 格式化纯函数（零依赖）
# ---------------------------------------------------------------------------

class TestFormatters:
    def test_history_to_text_truncates(self):
        history = [{"role": f"user{i}", "content": f"msg{i}"} for i in range(6)]
        text = history_to_text(history, limit=2)
        assert "user4" in text and "user0" not in text

    def test_format_results_empty_hint(self):
        assert format_results([], top_k=3, default_label="X", empty_hint="（空）") == "（空）"

    def test_format_milvus_tags_and_source(self):
        out = format_milvus_results(
            [{"content": "内容", "tool": "milvus", "metadata": {"title": "T"}}],
            top_k=2,
        )
        assert "[Milvus#1] 内容" in out and "来源：T" in out

    def test_format_postgres_label(self):
        out = format_postgres_results([{"content": "c", "tool": "postgres"}], top_k=1)
        assert out.startswith("[PostgreSQL#1]")

    def test_format_combined_local_default_label(self):
        out = format_combined_local_results([{"content": "c"}], top_k=1)
        assert out.startswith("[本地#1]")

    def test_format_external_uses_summary_and_url(self):
        out = format_external_results(
            [{"summary": "摘要", "metadata": {"url": "http://x"}}],
            top_k=1,
        )
        assert "[外部#1] 摘要" in out and "http://x" in out

    def test_collect_sources_dedup_preserves_order(self):
        docs = [
            {"source": "A"},
            {"metadata": {"source": "A"}},
            {"source_table": "B"},
        ]
        assert collect_sources(docs, []) == ["A", "B"]
