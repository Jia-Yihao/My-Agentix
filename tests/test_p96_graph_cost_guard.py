"""Phase H·W2 检索/图谱配置化验收（p96）。

覆盖规划 §4 W2 后两项：
1. Neo4j 成本护栏（``GraphCostEstimator`` 纯字符串预检）：``RETURN *`` /
   无界变长（``*..``、``*2..``、``[r*]``、``[:REL*]``）/ ``OPTIONAL MATCH``
   堆叠 → 拒绝；简单单 ``RETURN`` 尾无 ``LIMIT`` → 追加（limited）；
   有界变长 / UNION / 多 RETURN / 分号结尾 → 原样放行；
   ``query_readonly`` 接线：flag-off 逐位等价、flag-on 拒绝不触库、
   改写语句进 ``graph.query``、只读闸仍在最前；
2. ``MILVUS_NPROBE`` 配置化：默认 16 = 原硬编码值、可调、越界回落下限 1；
3. 配置治理：两键默认值、五处同步落点在位（settings / selfcheck /
   .env.example / 配置管理.md / p41 名单）。

运行：./.venv/Scripts/python.exe -m pytest tests/test_p96_graph_cost_guard.py -q
"""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentix.application.agents.kg_sub_graph.kg_neo4j_conn import (
    GraphCostEstimator,
    query_readonly,
)
from agentix.config import settings as cfg_settings


class TestDefaults:
    def test_flag_defaults(self):
        assert cfg_settings.NEO4J_COST_GUARD_ENABLED is False
        assert cfg_settings.MILVUS_NPROBE == 16


class TestGraphCostEstimator:
    def _est(self, stmt):
        return GraphCostEstimator.estimate(stmt)

    def test_return_star_rejected(self):
        assert self._est("MATCH (n) RETURN *")["verdict"] == "rejected"
        assert self._est("match (n)\nreturn * ")["reason"] == "return_star"

    @pytest.mark.parametrize(
        "stmt",
        [
            "MATCH (a)-[*]->(b) RETURN b",
            "MATCH (a)-[*2..]->(b) RETURN b",
            "MATCH p=(a)-[r*]->(b) RETURN p",
            "MATCH (a)-[:REL*]->(b) RETURN b",
        ],
    )
    def test_unbounded_var_length_rejected(self, stmt):
        out = self._est(stmt)
        assert out["verdict"] == "rejected"
        assert out["reason"] == "unbounded_var_length"

    @pytest.mark.parametrize(
        "stmt",
        [
            "MATCH (a)-[:REL*1..3]->(b) RETURN b",
            "MATCH (a)-[*..2]->(b) RETURN b",
        ],
    )
    def test_bounded_var_length_allowed(self, stmt):
        assert self._est(stmt)["verdict"] == "limited"  # 仅追加 LIMIT

    def test_optional_match_flood_rejected(self):
        ok = " OPTIONAL MATCH (x1) " * 4 + "MATCH (n) RETURN n"
        flood = " OPTIONAL MATCH (x1) " * 5 + "MATCH (n) RETURN n"
        assert self._est(ok)["verdict"] == "limited"
        assert self._est(flood) == {
            "verdict": "rejected",
            "reason": "optional_match_flood",
            "cypher": flood.strip(),
        }

    def test_simple_return_gets_limit_appended(self):
        out = self._est("MATCH (n:Article) WHERE n.no = '38' RETURN n")
        assert out["verdict"] == "limited"
        assert out["cypher"].endswith(" LIMIT 1000")

    @pytest.mark.parametrize(
        "stmt",
        [
            "MATCH (n) RETURN n LIMIT 10",
            "CALL { MATCH (x) RETURN x } YIELD x RETURN x",  # 多 RETURN（子查询）
            "MATCH (n) RETURN n UNION MATCH (m) RETURN m",
            "MATCH (n) RETURN n;",
            "CALL { MATCH (n) RETURN n } RETURN 1",
        ],
    )
    def test_complex_statements_left_unchanged(self, stmt):
        out = self._est(stmt)
        assert out["verdict"] == "ok"
        assert out["cypher"] == stmt.strip()

    def test_no_return_left_unchanged(self):
        stmt = "MATCH (n) WHERE n.x IS NOT NULL"
        assert self._est(stmt)["verdict"] == "ok"


class TestQueryReadonlyWiring:
    def _graph(self):
        graph = MagicMock()
        graph.query.return_value = ["row"]
        return graph

    def test_flag_off_passes_statement_through_unchanged(self):
        graph = self._graph()
        stmt = "MATCH (a)-[*]->(b) RETURN *"
        out = query_readonly(graph, stmt)  # 默认关：零预检
        assert out == ["row"]
        graph.query.assert_called_once_with(stmt)

    def test_flag_off_preserves_params_call_shape(self):
        graph = self._graph()
        query_readonly(graph, "MATCH (n) RETURN n", params={"k": 1})
        graph.query.assert_called_once_with(query="MATCH (n) RETURN n", params={"k": 1})

    def test_flag_on_rejects_without_touching_db(self, monkeypatch):
        monkeypatch.setattr(cfg_settings, "NEO4J_COST_GUARD_ENABLED", True)
        graph = self._graph()
        with pytest.raises(ValueError, match="cost guard"):
            query_readonly(graph, "MATCH (n) RETURN *")
        graph.query.assert_not_called()

    def test_flag_on_appends_limit_before_exec(self, monkeypatch):
        monkeypatch.setattr(cfg_settings, "NEO4J_COST_GUARD_ENABLED", True)
        graph = self._graph()
        query_readonly(graph, "MATCH (n) RETURN n")
        graph.query.assert_called_once_with("MATCH (n) RETURN n LIMIT 1000")

    def test_readonly_gate_still_fires_first(self, monkeypatch):
        monkeypatch.setattr(cfg_settings, "NEO4J_COST_GUARD_ENABLED", True)
        graph = self._graph()
        with pytest.raises(ValueError, match="write clause"):
            query_readonly(graph, "MATCH (n) DETACH DELETE n")
        graph.query.assert_not_called()

    def test_flag_on_passes_ok_statement_unchanged(self, monkeypatch):
        monkeypatch.setattr(cfg_settings, "NEO4J_COST_GUARD_ENABLED", True)
        graph = self._graph()
        stmt = "MATCH (n) RETURN n LIMIT 5"
        query_readonly(graph, stmt)
        graph.query.assert_called_once_with(stmt)


class TestMilvusNprobe:
    def _store(self, monkeypatch, nprobe=None):
        from agentix.retrieval.infra.vectorstore.milvus_impl import MilvusVectorStore

        if nprobe is not None:
            monkeypatch.setattr(cfg_settings, "MILVUS_NPROBE", nprobe)
        store = MilvusVectorStore.__new__(MilvusVectorStore)
        store._collection = MagicMock()
        store._collection.search.return_value = []
        store._encode_text = lambda q: [0.0] * 4
        return store

    def _last_params(self, store):
        kwargs = store._collection.search.call_args.kwargs
        return kwargs["param"]["params"]["nprobe"]

    def test_default_is_16_behavior_equal(self, monkeypatch):
        store = self._store(monkeypatch)  # 不改值 = 默认 16
        store.similarity_search("query", k=3)
        assert self._last_params(store) == 16

    def test_configurable_nprobe(self, monkeypatch):
        store = self._store(monkeypatch, nprobe=8)
        store.similarity_search("query", k=3)
        assert self._last_params(store) == 8

    @pytest.mark.parametrize("bad,expected", [(0, 1), (-3, 1)])
    def test_out_of_range_clamps_to_one(self, monkeypatch, bad, expected):
        store = self._store(monkeypatch, nprobe=bad)
        store.similarity_search("query", k=3)
        assert self._last_params(store) == expected

    def test_non_numeric_falls_back_to_default(self, monkeypatch):
        store = self._store(monkeypatch, nprobe="abc")
        store.similarity_search("query", k=3)
        assert self._last_params(store) == 16


class TestGovernanceSync:
    """W2 两键的五处同步落点（settings 已由 Defaults 覆盖）。"""

    ROOT = Path(__file__).resolve().parents[1]

    def test_selfcheck_ignores_missing(self):
        from agentix.config.selfcheck import DEFAULT_IGNORED_MISSING

        assert "NEO4J_COST_GUARD_ENABLED" in DEFAULT_IGNORED_MISSING
        assert "MILVUS_NPROBE" in DEFAULT_IGNORED_MISSING

    def test_env_example_declares_active_lines(self):
        env = (self.ROOT / ".env.example").read_text(encoding="utf-8")
        assert "\nMILVUS_NPROBE=16" in env
        assert "\nNEO4J_COST_GUARD_ENABLED=false" in env

    def test_retrieval_flags_commented_in_env_example(self):
        env = (self.ROOT / ".env.example").read_text(encoding="utf-8")
        for key in (
            "RETRIEVAL_SINGLEFLIGHT_ENABLED",
            "RETRIEVAL_ADAPTIVE_ENABLED",
            "RETRIEVAL_CASCADE_MIN_SCORE",
            "RERANK_DYNAMIC_SKIP_ENABLED",
        ):
            assert f"\n# {key}=" in env  # 检索域惯例：注释示例行

    def test_configuration_md_documents_two_keys(self):
        doc = (
            self.ROOT / "docs/architecture/配置管理.md"
        ).read_text(encoding="utf-8")
        assert "`NEO4J_COST_GUARD_ENABLED`" in doc
        assert "`MILVUS_NPROBE`" in doc

    def test_p41_tuple_contains_two_keys(self):
        p41 = (self.ROOT / "tests/test_p41_doc_consistency.py").read_text(
            encoding="utf-8"
        )
        assert '"NEO4J_COST_GUARD_ENABLED"' in p41
        assert '"MILVUS_NPROBE"' in p41
