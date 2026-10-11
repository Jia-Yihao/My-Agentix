"""P1-3 工具注册表单测。

覆盖方案 P1-3 验收标准：
1. 注册表装配：ensure_registered 幂等、4 工具 spec 齐全、factory 返回 schema 类
2. 每个 spec 的 description 都含「何时用 / 何时不能用」反例（验收 2）
3. get_active_tools 按 settings.DISABLED_TOOLS 裁剪（注册表 spec 不受影响）
4. tool_selection 查表分发：_resolve_route 4 分支 + AgentError(TOOL_NOT_FOUND)（验收 3）
5. 节点级集成：AIMessage tool_calls -> Command(Send) 行为保持（含 cypher_query
   历史 payload 契约与 text2sql 启发式快路径）
"""

import asyncio
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables.base import RunnableLambda

from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.tool_selection.node import (
    _TOOL_ROUTE_TABLE,
    _resolve_route,
    create_tool_selection_node,
)
from agentix.config import settings
from agentix.domain.errors import AgentError, ErrorCode
from agentix.tools.definitions import registry
from agentix.tools.registry_init import (
    ensure_registered,
    get_active_tools,
)


@pytest.fixture(autouse=True)
def _registered():
    ensure_registered()
    yield


class TestRegistrySetup:
    def test_idempotent_registration(self):
        """ensure_registered 幂等：重复调用不重复注册、不报错。"""
        ensure_registered()
        ensure_registered()
        kg_names = [
            "cypher_query",
            "predefined_cypher",
            "microsoft_graphrag_query",
            "text2sql_query",
        ]
        all_names = list(registry.tool_names())
        # 4 个 KG 工具各注册一次（不重复）；registry 全集可能含历史遗留
        # search（infrastructure/tools/__init__.py 注册），只断言子集关系
        assert all(all_names.count(n) == 1 for n in kg_names)

    def test_all_four_tools_registered(self):
        assert set(registry.tool_names()) >= {
            "cypher_query",
            "predefined_cypher",
            "microsoft_graphrag_query",
            "text2sql_query",
        }

    def test_descriptions_contain_counterexamples(self, monkeypatch):
        """方案 P1-3 验收 2：每个 KG spec 的 description 都含「何时不能用」反例。

        历史遗留的 search spec 不在本轮范围，仅校验 4 个 KG 工具。
        """
        monkeypatch.setattr(settings, "DISABLED_TOOLS", "")
        for schema_cls in get_active_tools():
            spec = registry.get_spec(schema_cls.model_json_schema()["title"])
            assert "何时用" in spec.description, spec.name
            assert "何时不能用" in spec.description, spec.name

    def test_factory_returns_schema_class(self):
        from agentix.application.agents.kg_sub_graph.kg_tools_list import cypher_query

        assert registry.create("cypher_query") is cypher_query

    def test_spec_parameters_match_schema(self):
        """spec.parameters 即 schema 的 JSON Schema（供 OpenAI function calling）。"""
        spec = registry.get_spec("text2sql_query")
        assert spec.parameters.get("title") == "text2sql_query"


class TestGetActiveTools:
    def test_default_all_tools(self, monkeypatch):
        monkeypatch.setattr(settings, "DISABLED_TOOLS", "")
        assert len(get_active_tools()) == 4

    def test_disabled_tools_pruned(self, monkeypatch):
        """DISABLED_TOOLS 裁剪生效；注册表 spec 不受影响（可随时恢复）。"""
        monkeypatch.setattr(
            settings, "DISABLED_TOOLS", "text2sql_query, predefined_cypher"
        )
        names = {t.model_json_schema().get("title") for t in get_active_tools()}
        assert names == {"cypher_query", "microsoft_graphrag_query"}
        # 注册表本身不裁剪
        assert registry.has_tool("text2sql_query")

    def test_unknown_disabled_name_ignored(self, monkeypatch):
        monkeypatch.setattr(settings, "DISABLED_TOOLS", "nonexistent_tool")
        assert len(get_active_tools()) == 4


class TestResolveRoute:
    def test_direct_table_entries(self):
        assert _resolve_route("predefined_cypher", set()) == (
            "predefined_cypher",
            "args",
        )
        assert _resolve_route("cypher_query", set()) == ("cypher_query", "question")
        assert _resolve_route("text2sql_query", set()) == ("text2sql_query", "args")
        assert set(_TOOL_ROUTE_TABLE) == {
            "predefined_cypher",
            "cypher_query",
            "text2sql_query",
        }

    def test_dynamic_tool_falls_back_to_customer_tools(self):
        """直连表未命中但已注册的动态工具 → customer_tools。"""
        assert _resolve_route(
            "microsoft_graphrag_query", {"microsoft_graphrag_query"}
        ) == ("customer_tools", "args")

    def test_unregistered_raises_agent_error(self):
        """方案 P1-3 验收 3：未注册工具名 -> AgentError(TOOL_NOT_FOUND) 非静默降级。"""
        with pytest.raises(AgentError) as exc_info:
            _resolve_route("unknown_tool", {"microsoft_graphrag_query"})
        assert exc_info.value.code is ErrorCode.TOOL_NOT_FOUND


def _make_node(tool_name: str, tool_args: dict):
    msg = AIMessage(
        content="",
        tool_calls=[{"name": tool_name, "args": tool_args, "id": "call_1"}],
    )
    llm = MagicMock()
    llm.bind_tools.return_value = RunnableLambda(lambda _input: msg)
    return create_tool_selection_node(llm, list(get_active_tools()))


def _run(coro):
    return asyncio.run(coro)


class TestToolSelectionNodeDispatch:
    def test_predefined_cypher_payload(self):
        node = _make_node(
            "predefined_cypher", {"query": "article_by_no", "parameters": {}}
        )
        result = _run(node({"question": "查民法典第577条的内容"}))
        assert result.goto.node == "predefined_cypher"
        assert result.goto.arg["query_name"] == "predefined_cypher"
        assert result.goto.arg["query_parameters"] == {
            "query": "article_by_no",
            "parameters": {},
        }

    def test_cypher_query_keeps_legacy_payload(self):
        """cypher_query 历史契约：query_parameters 固定 {"question": 原问题}。"""
        node = _make_node("cypher_query", {"task": "查民法典第577条的内容"})
        result = _run(node({"question": "查民法典第577条的内容"}))
        assert result.goto.node == "cypher_query"
        assert result.goto.arg["query_parameters"] == {
            "question": "查民法典第577条的内容"
        }

    def test_text2sql_query_payload(self):
        node = _make_node("text2sql_query", {"task": "统计法规总数"})
        result = _run(node({"question": "全国有多少部法规"}))
        assert result.goto.node == "text2sql_query"
        assert result.goto.arg["query_name"] == "text2sql_query"

    def test_graphrag_goes_to_customer_tools(self):
        node = _make_node("microsoft_graphrag_query", {"query": "综合分析违约责任"})
        result = _run(node({"question": "综合分析合同违约的责任竞合"}))
        assert result.goto.node == "customer_tools"
        assert result.goto.arg["query_name"] == "microsoft_graphrag_query"

    def test_text2sql_heuristic_fast_path(self):
        """route_type=text2sql-query + SQL 关键词 → 启发式快路径直发（不经 LLM）。"""
        llm = MagicMock()
        llm.bind_tools.return_value = RunnableLambda(
            lambda _i: pytest.fail("LLM should not be called on fast path")
        )
        node = create_tool_selection_node(llm, list(get_active_tools()))
        result = _run(
            node(
                {
                    "question": "统计各年份发布的法规数量",
                    "context": {"route_type": "text2sql-query"},
                }
            )
        )
        assert result.goto.node == "text2sql_query"

    def test_descriptive_heuristic_fast_path(self):
        """描述类关键词（释义等）→ customer_tools 快路径（行为保持）。"""
        llm = MagicMock()
        llm.bind_tools.return_value = RunnableLambda(
            lambda _i: pytest.fail("LLM should not be called on fast path")
        )
        node = create_tool_selection_node(llm, list(get_active_tools()))
        result = _run(node({"question": "民法典第577条的立法释义是什么"}))
        assert result.goto.node == "customer_tools"
        assert result.goto.arg["query_name"] == "microsoft_graphrag_query"
