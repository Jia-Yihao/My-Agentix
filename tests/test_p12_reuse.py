"""P1-2 连接复用与编译缓存单测。

覆盖：
- kg_neo4j_conn: 进程内单例（双检锁）、失败不缓存自动重试、reset 重建、
  构造期 refresh_schema=False（避免每次实例化扫全库 schema）、并发首调用
- lg_builder._get_compiled_multi_tool_workflow: 配置指纹缓存命中/miss、
  llm/graph 运行时对象不参与缓存 key（方案 P1-2 验收标准 3 的单测化）
"""

import threading
from unittest.mock import MagicMock, patch

import pytest

import agentix.application.agents.kg_sub_graph.kg_neo4j_conn as kg_conn
import agentix.orchestration.langgraph.lg_builder as lg_builder
from agentix.application.agents.kg_sub_graph.kg_tools_list import (
    cypher_query,
    microsoft_graphrag_query,
    predefined_cypher,
    text2sql_query,
)


@pytest.fixture(autouse=True)
def _isolate_singletons():
    """每个用例前后清理单例/编译缓存状态，避免跨用例污染。"""
    kg_conn.reset_neo4j_graph()
    lg_builder._get_compiled_multi_tool_workflow.cache_clear()
    yield
    kg_conn.reset_neo4j_graph()
    lg_builder._get_compiled_multi_tool_workflow.cache_clear()


def _make_graph():
    graph = MagicMock()
    graph.refresh_schema = MagicMock(return_value=None)
    return graph


class TestNeo4jSingleton:
    def test_singleton_reuses_instance(self):
        """多次 get_neo4j_graph 只构造一次 Neo4jGraph。"""
        graph = _make_graph()
        with patch.object(kg_conn, "Neo4jGraph", return_value=graph) as ctor:
            g1 = kg_conn.get_neo4j_graph()
            g2 = kg_conn.get_neo4j_graph()
            g3 = kg_conn.get_neo4j_graph()
        assert g1 is g2 is g3
        assert ctor.call_count == 1
        # 首建刷一次 schema（兼做连通性健康检查）
        graph.refresh_schema.assert_called_once()

    def test_refresh_schema_disabled_on_construction(self):
        """构造参数 refresh_schema=False（性能关键：避免每次实例化扫全库）。"""
        graph = _make_graph()
        with patch.object(kg_conn, "Neo4jGraph", return_value=graph) as ctor:
            kg_conn.get_neo4j_graph()
        kwargs = ctor.call_args.kwargs
        assert kwargs["refresh_schema"] is False

    def test_auth_kwargs_passed_when_configured(self):
        """配置了用户名密码时传参；未配置时不传。"""
        graph = _make_graph()
        with patch.object(kg_conn, "Neo4jGraph", return_value=graph) as ctor:
            kg_conn.get_neo4j_graph()
        kwargs = ctor.call_args.kwargs
        assert "url" in kwargs and "database" in kwargs

    def test_reset_forces_rebuild(self):
        """reset_neo4j_graph 后下次调用重建新实例。"""
        g1, g2 = MagicMock(), MagicMock()
        with patch.object(kg_conn, "Neo4jGraph", side_effect=[g1, g2]):
            first = kg_conn.get_neo4j_graph()
            kg_conn.reset_neo4j_graph()
            second = kg_conn.get_neo4j_graph()
        assert first is g1
        assert second is g2
        assert first is not second

    def test_failure_not_cached_next_call_retries(self):
        """首建失败（refresh_schema 抛异常）不缓存失败状态，下次自动重试。"""
        bad = MagicMock()
        bad.refresh_schema.side_effect = RuntimeError("connection refused")
        good = MagicMock()
        with patch.object(kg_conn, "Neo4jGraph", side_effect=[bad, good]):
            with pytest.raises(RuntimeError, match="connection refused"):
                kg_conn.get_neo4j_graph()
            # 失败状态不落缓存
            assert kg_conn._graph_cache is None
            recovered = kg_conn.get_neo4j_graph()
        assert recovered is good

    def test_concurrent_first_call_single_construction(self):
        """并发首调用：双检锁保证只建连一次。"""
        graph = _make_graph()
        barrier = threading.Barrier(8)
        results = []

        def worker():
            barrier.wait()
            results.append(kg_conn.get_neo4j_graph())

        with patch.object(kg_conn, "Neo4jGraph", return_value=graph) as ctor:
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        assert ctor.call_count == 1
        assert all(r is graph for r in results)


class TestCompiledWorkflowCache:
    KEY = (cypher_query, predefined_cypher, microsoft_graphrag_query, text2sql_query)

    def test_cache_hit_same_object(self):
        """相同配置指纹 → 命中缓存，返回同一编译图对象（只编译一次）。"""
        compiled = MagicMock(name="compiled_graph")
        with patch.object(
            lg_builder, "create_multi_tool_workflow", return_value=compiled
        ) as mw, patch.object(
            lg_builder, "get_neo4j_graph", return_value=MagicMock()
        ), patch.object(
            lg_builder, "get_llm_client", return_value=MagicMock()
        ):
            w1 = lg_builder._get_compiled_multi_tool_workflow(self.KEY, "scope", True)
            w2 = lg_builder._get_compiled_multi_tool_workflow(self.KEY, "scope", True)
        assert w1 is w2 is compiled
        assert mw.call_count == 1

    def test_different_key_compiles_new_graph(self):
        """配置指纹变化（tool_schemas 元组）→ 缓存 miss，新图生效（验收 3）。"""
        c1, c2 = MagicMock(name="c1"), MagicMock(name="c2")
        with patch.object(
            lg_builder, "create_multi_tool_workflow", side_effect=[c1, c2]
        ) as mw, patch.object(
            lg_builder, "get_neo4j_graph", return_value=MagicMock()
        ), patch.object(
            lg_builder, "get_llm_client", return_value=MagicMock()
        ):
            w1 = lg_builder._get_compiled_multi_tool_workflow(self.KEY, "scope", True)
            w2 = lg_builder._get_compiled_multi_tool_workflow(
                self.KEY[:2], "scope", True
            )
        assert w1 is c1 and w2 is c2
        assert mw.call_count == 2

    def test_runtime_objects_not_in_key(self):
        """llm/graph 等运行时对象不参与 key：指纹相同即命中，
        闭包只执行一次（get_neo4j_graph/get_llm_client 各调用一次）。"""
        compiled = MagicMock()
        with patch.object(
            lg_builder, "create_multi_tool_workflow", return_value=compiled
        ), patch.object(
            lg_builder, "get_neo4j_graph", return_value=MagicMock()
        ) as graph_factory, patch.object(
            lg_builder, "get_llm_client", return_value=MagicMock()
        ) as llm_factory:
            w1 = lg_builder._get_compiled_multi_tool_workflow(self.KEY, "scope", True)
            w2 = lg_builder._get_compiled_multi_tool_workflow(self.KEY, "scope", True)
        assert w1 is w2
        # 缓存命中时工厂闭包不再执行，运行时对象获取也随之跳过
        assert graph_factory.call_count == 1
        assert llm_factory.call_count == 1

    def test_compiled_graph_receives_singleton_graph(self):
        """编译时 graph 参数来自 get_neo4j_graph 单例（连接复用贯通）。"""
        singleton_graph = MagicMock()
        with patch.object(
            lg_builder, "create_multi_tool_workflow", return_value=MagicMock()
        ) as mw, patch.object(
            lg_builder, "get_neo4j_graph", return_value=singleton_graph
        ), patch.object(
            lg_builder, "get_llm_client", return_value=MagicMock()
        ):
            lg_builder._get_compiled_multi_tool_workflow(self.KEY, "scope", True)
        assert mw.call_args.kwargs["graph"] is singleton_graph
        assert mw.call_args.kwargs["tool_schemas"] == list(self.KEY)
