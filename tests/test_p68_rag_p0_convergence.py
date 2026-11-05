"""RAG P0 收敛契约测试（Stage B2 / R-P0-1 … R-P0-5）。

覆盖五项 P0：

* **R-P0-1 LightRAG 去重**——``customer_tools/node.py`` 的 ``LightRAGAPI``
  是 ``agentix/application/services/lightrag_service.py`` 单实现之上的兼容
  门面：构造参数转调、query/insert 返回 dict 形状、异常前缀均与历史一致，
  且节点源码内不再保留平行的 LightRAG 构造/embedding/查询逻辑
  （另见 tests/test_lightrag_node.py 的节点与配置契约）。
* **R-P0-2 租户 fail-closed**——``RetrievalPortAdapter`` 三方法对空
  ``tenant_id`` 在触达任何存储访问之前抛 ``ValueError``（契约违规=响亮失败）；
  基础设施故障仍走既有 ``degraded`` 语义，两者不可混淆。
* **R-P0-3 rerank 配置三态收敛**——``RerankerCompatStub.enabled`` 改
  ``None`` 三态（None=未决，不再回落 True）；旧键
  ``KB_RERANK_SCORE_THRESHOLD`` 经 validator 映射到
  ``KB_POSTGRES_RERANK_THRESHOLD`` 并告警（新键胜出）。
* **R-P0-4 只读闸 + Neo4j schema TTL**——公共 ``query_readonly``：写子句
  在触库之前 ``ValueError`` fail-closed（执行缝全接线），``timeout<=0``
  同步逐位等价调用形状、``>0`` 走 asyncio 超时；schema 快路径带 TTL
  刷新（失败保留旧 schema + warning）；dynamic 分类显式
  ``NotImplementedError`` 降级为通用示例（不再静默关键词分类）。
* **R-P0-5 打点不吞错**——``_record_rag_retrieval`` 返回 bool，失败
  warning + ``record_degraded`` 计数显式透出且永不抛出；chat 侧
  ``record_retrieval`` 包裹同语义；finalize 保留通道 ``degraded`` 位与
  双通道原文（为 P1 融合留证据）。
"""

from __future__ import annotations

import importlib
import inspect
import logging
import re
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentix.retrieval.usecases.retrieval_port_adapter import RetrievalPortAdapter

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE_FILE_REL = Path(
    "agentix/application/agents/kg_sub_graph/agentic_rag_agents/"
    "components/customer_tools/node.py"
)
NODE_MODULE_PATH = (
    "agentix.application.agents.kg_sub_graph.agentic_rag_agents."
    "components.customer_tools.node"
)
SERVICE_MODULE = "agentix.application.services.lightrag_service"

lightrag_node = importlib.import_module(NODE_MODULE_PATH)

requires_lightrag = pytest.mark.skipif(
    not lightrag_node.LIGHTRAG_AVAILABLE,
    reason="lightrag-hku 未安装（pip install lightrag-hku）",
)


# ---------------------------------------------------------------------------
# R-P0-1：LightRAG 单实现去重（源码级守卫）
# ---------------------------------------------------------------------------


class TestLightRAGDedupSource:
    """源码级守卫：平行实现不许再长回来。"""

    def test_node_source_has_no_parallel_light_rag_impl(self):
        src = (REPO_ROOT / NODE_FILE_REL).read_text(encoding="utf-8")
        # 门面必须转调单实现
        assert "LightRAGService" in src
        # 平行实现的历史指纹（构造/查询参数/embedding/插入）全部不许出现
        banned = (
            "LightRAG(",
            "QueryParam(",
            "EmbeddingFunc(",
            "openai_embed(",
            "openai_complete_if_cache(",
            "ainsert(",
        )
        for fragment in banned:
            assert fragment not in src, f"node.py 残留平行实现片段: {fragment}"

    def test_light_rag_constructor_call_only_in_service(self):
        pattern = re.compile(r"(?<![\w.])LightRAG\(")
        offenders = []
        for path in sorted((REPO_ROOT / "agentix").rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if pattern.search(text):
                offenders.append(path.relative_to(REPO_ROOT).as_posix())
        assert offenders == ["agentix/application/services/lightrag_service.py"]

    def test_graphrag_alias_is_same_object(self):
        assert lightrag_node.GraphRAGAPI is lightrag_node.LightRAGAPI
        assert lightrag_node.create_graphrag_query_node is (
            lightrag_node.create_lightrag_query_node
        )


# ---------------------------------------------------------------------------
# R-P0-1：门面行为契约（转调不改形状）
# ---------------------------------------------------------------------------


@requires_lightrag
class TestLightRAGFacadeContract:
    """构造签名/返回 dict/异常前缀与历史 LightRAGAPI 逐项一致。"""

    def test_ctor_defaults_and_lazy_service(self):
        from agentix.config import settings

        api = lightrag_node.LightRAGAPI()
        assert api.working_dir == settings.LIGHTRAG_WORKING_DIR
        assert api.retrieval_mode == settings.LIGHTRAG_RETRIEVAL_MODE
        assert api.top_k == settings.LIGHTRAG_TOP_K
        assert api.max_token_size == settings.LIGHTRAG_MAX_TOKEN_SIZE
        assert api.initialized is False
        assert api.rag is None
        # 构造阶段不触发单实现（延迟导入）
        assert api._svc is None

    @pytest.mark.asyncio
    async def test_query_facade_preserves_dict_contract(self, monkeypatch):
        captured: dict = {"ctors": []}

        class _FakeService:
            rag = "底层RAG实例"

            def __init__(self, **kwargs):
                captured["ctors"].append(kwargs)

            async def initialize(self):
                captured["inits"] = captured.get("inits", 0) + 1

            async def query(self, query, mode=None, top_k=None, stream=False):
                captured["query"] = {
                    "query": query,
                    "mode": mode,
                    "top_k": top_k,
                    "stream": stream,
                }
                return "法条答案"

        monkeypatch.setattr(f"{SERVICE_MODULE}.LightRAGService", _FakeService)

        api = lightrag_node.LightRAGAPI(
            working_dir="./w",
            retrieval_mode="local",
            top_k=3,
            max_token_size=512,
        )
        result = await api.query("什么是善意取得")

        assert result == {
            "response": "法条答案",
            "mode": "local",
            "query": "什么是善意取得",
        }
        # 构造参数逐项转调单实现（历史 max_token_size 语义不再丢失）
        assert captured["ctors"] == [
            {
                "working_dir": "./w",
                "default_mode": "local",
                "default_top_k": 3,
                "max_token_size": 512,
            }
        ]
        assert captured["query"] == {
            "query": "什么是善意取得",
            "mode": "local",
            "top_k": 3,
            "stream": False,
        }
        assert api.initialized is True
        assert api.rag == "底层RAG实例"

    @pytest.mark.asyncio
    async def test_query_mode_override_and_error_not_double_wrapped(self, monkeypatch):
        class _BoomService:
            rag = None

            def __init__(self, **kwargs):
                pass

            async def initialize(self):
                pass

            async def query(self, query, mode=None, top_k=None, stream=False):
                raise RuntimeError(f"LightRAG 查询失败: 上游超时({mode})")

        monkeypatch.setattr(f"{SERVICE_MODULE}.LightRAGService", _BoomService)

        api = lightrag_node.LightRAGAPI(retrieval_mode="local")
        with pytest.raises(RuntimeError) as exc_info:
            await api.query("q", mode="hybrid")

        message = str(exc_info.value)
        # 单实现已带历史前缀，门面不许再包一层
        assert message == "LightRAG 查询失败: 上游超时(hybrid)"
        assert message.count("LightRAG 查询失败") == 1

    @pytest.mark.asyncio
    async def test_insert_documents_maps_failed_to_error(self, monkeypatch):
        class _InsertService:
            rag = None

            def __init__(self, **kwargs):
                pass

            async def initialize(self):
                pass

            async def insert_documents(self, documents, batch_size=10):
                return SimpleNamespace(
                    total=len(documents),
                    success=2,
                    failed=1,
                    errors=["文档 3 插入失败: boom"],
                )

        monkeypatch.setattr(f"{SERVICE_MODULE}.LightRAGService", _InsertService)

        api = lightrag_node.LightRAGAPI()
        result = await api.insert_documents(["a", "b", "c"])

        # 历史形状 {"total", "success", "error"}（error=失败计数）保持不变，
        # scripts/init_lightrag.py 依赖这三个键。
        assert result == {"total": 3, "success": 2, "error": 1}


# ---------------------------------------------------------------------------
# R-P0-3：Rerank 三态同源与阈值键收敛
# ---------------------------------------------------------------------------


class TestRerankTriStateConvergence:
    """网关兼容桩与内层开关同取 RERANKER_ENABLED；旧阈值键弃用映射。"""

    def test_stub_enabled_follows_single_switch(self, monkeypatch):
        from agentix.application.services.knowledge_gateway import (
            _RerankerCompatStub,
        )
        from agentix.retrieval.settings import settings as rs_settings

        monkeypatch.setattr(rs_settings, "RERANKER_ENABLED", False)
        assert _RerankerCompatStub().enabled is False

        monkeypatch.setattr(rs_settings, "RERANKER_ENABLED", True)
        assert _RerankerCompatStub().enabled is True

        # 显式参数覆盖开关（测试注入用）
        assert _RerankerCompatStub(enabled=False).enabled is False

    @pytest.mark.asyncio
    async def test_stub_rerank_stays_passthrough(self):
        """「开」态也不在外层重复调用外部服务：桩 rerank 直通。"""
        from agentix.application.services.knowledge_gateway import (
            _RerankerCompatStub,
        )

        stub = _RerankerCompatStub(enabled=True)
        docs = [{"id": 1}, {"id": 2}]
        assert await stub.rerank("q", docs, 5) is docs
        assert stub.enabled is True

    def test_deprecated_threshold_key_maps_when_new_key_unset(
        self, monkeypatch, caplog
    ):
        import logging

        from agentix.config.settings import Settings

        monkeypatch.delenv("KB_POSTGRES_RERANK_THRESHOLD", raising=False)
        monkeypatch.delenv("KB_RERANK_SCORE_THRESHOLD", raising=False)

        with caplog.at_level(
            logging.WARNING, logger="agentix.config.settings"
        ):
            merged = Settings(
                _env_file=None, KB_RERANK_SCORE_THRESHOLD=0.9
            )

        # 旧键值映射到现役键（兼容映射）
        assert merged.KB_POSTGRES_RERANK_THRESHOLD == 0.9
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "KB_RERANK_SCORE_THRESHOLD is deprecated" in m for m in messages
        ), messages
        assert any("mapping its value" in m for m in messages), messages

    def test_deprecated_threshold_key_loses_when_new_key_set(
        self, monkeypatch, caplog
    ):
        import logging

        from agentix.config.settings import Settings

        with caplog.at_level(
            logging.WARNING, logger="agentix.config.settings"
        ):
            merged = Settings(
                _env_file=None,
                KB_RERANK_SCORE_THRESHOLD=0.9,
                KB_POSTGRES_RERANK_THRESHOLD=0.7,
            )

        # 新键显式值胜出（旧键被忽略）
        assert merged.KB_POSTGRES_RERANK_THRESHOLD == 0.7
        messages = [r.getMessage() for r in caplog.records]
        assert any("is set explicitly and wins" in m for m in messages), messages

    def test_no_code_reads_deprecated_threshold_key(self):
        """弃用键只许出现在 settings 声明/映射处，不许出现消费点。"""
        offenders = []
        for path in sorted((REPO_ROOT / "agentix").rglob("*.py")):
            if path.relative_to(REPO_ROOT).as_posix() == (
                "agentix/config/settings.py"
            ):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if ".KB_RERANK_SCORE_THRESHOLD" in text:
                offenders.append(path.relative_to(REPO_ROOT).as_posix())
        assert offenders == []

    def test_postgres_branch_reads_live_threshold_key(self):
        src = (
            REPO_ROOT
            / "agentix/application/agents/kg_sub_graph/agentic_rag_agents/"
            "workflows/multi_agent/kb_retrieval.py"
        ).read_text(encoding="utf-8")
        assert "KB_POSTGRES_RERANK_THRESHOLD" in src
        assert "KB_RERANK_SCORE_THRESHOLD" not in src


# ---------------------------------------------------------------------------
# R-P0-2：租户 fail-closed
# ---------------------------------------------------------------------------


class _ExplodingSessionScope:
    """触达即失败的 session 工厂。

    双重用途：fail-closed 测试里它**不该**被触达（否则 pytest.raises(ValueError)
    失败）；降级语义测试里它模拟基础设施故障（assert 走到 degraded）。
    """

    def __call__(self):
        raise AssertionError("storage unavailable")


class TestTenantFailClosed:
    """空 tenant_id ⇒ ValueError（存储访问之前）；正常租户语义不变。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_tenant", ["", "   ", None])
    async def test_search_fails_closed_before_storage_access(self, bad_tenant):
        adapter = RetrievalPortAdapter(session_scope_factory=_ExplodingSessionScope())

        with pytest.raises(ValueError, match="tenant_id"):
            await adapter.search(
                query="什么是善意取得",
                kb_id="kb1",
                tenant_id=bad_tenant,  # type: ignore[arg-type]
            )

    @pytest.mark.asyncio
    async def test_delete_and_index_status_fail_closed(self):
        adapter = RetrievalPortAdapter(session_scope_factory=_ExplodingSessionScope())

        with pytest.raises(ValueError, match="tenant_id"):
            await adapter.delete_by_doc(doc_id="d1", tenant_id="")

        with pytest.raises(ValueError, match="tenant_id"):
            await adapter.index_status(kb_id="kb1", tenant_id="  ")

    @pytest.mark.asyncio
    async def test_valid_tenant_delete_reaches_storage(self):
        """有租户时校验放行，存储访问照常发生（对照组）。"""
        adapter = RetrievalPortAdapter(session_scope_factory=_ExplodingSessionScope())

        with pytest.raises(AssertionError, match="storage unavailable"):
            await adapter.delete_by_doc(doc_id="d1", tenant_id="t1")

    @pytest.mark.asyncio
    async def test_valid_tenant_keeps_degraded_semantics_for_infra_failure(self):
        """契约违规=响亮失败；基础设施故障=degraded——两者不许混淆。"""
        adapter = RetrievalPortAdapter(session_scope_factory=_ExplodingSessionScope())

        result = await adapter.search(
            query="什么是善意取得",
            kb_id="kb1",
            tenant_id="t1",
        )

        assert result["status"] == "degraded"
        assert result["error"] == "retrieval_unavailable"

    def test_tenant_id_is_required_keyword_only_on_port_methods(self):
        for name in ("search", "delete_by_doc", "index_status"):
            param = inspect.signature(
                getattr(RetrievalPortAdapter, name)
            ).parameters["tenant_id"]
            assert param.kind is inspect.Parameter.KEYWORD_ONLY, name
            assert param.default is inspect.Parameter.empty, name


# ---------------------------------------------------------------------------
# R-P0-4：图谱硬化最小集
# ---------------------------------------------------------------------------


class TestNeo4jSchemaRefreshTtl:
    """单例不再永久持旧 schema：TTL 到期可刷新，刷新失败保留旧 schema。"""

    @pytest.fixture(autouse=True)
    def _isolate_singleton(self):
        import agentix.application.agents.kg_sub_graph.kg_neo4j_conn as kg_conn

        kg_conn.reset_neo4j_graph()
        yield kg_conn
        kg_conn.reset_neo4j_graph()

    @staticmethod
    def _prime_cache(kg_conn, monkeypatch, refreshed_age: float = 10_000.0):
        """注入缓存实例与「久未刷新」的时间戳（不触达真实建连）。"""
        graph = MagicMock()
        graph.refresh_schema.return_value = None
        monkeypatch.setattr(kg_conn, "_graph_cache", graph)
        monkeypatch.setattr(
            kg_conn, "_schema_refreshed_at", time.monotonic() - refreshed_age
        )
        return graph

    def test_ttl_expiry_triggers_schema_refresh(self, _isolate_singleton, monkeypatch):
        kg_conn = _isolate_singleton
        graph = self._prime_cache(kg_conn, monkeypatch, refreshed_age=10_000.0)
        monkeypatch.setattr(kg_conn.settings, "NEO4J_SCHEMA_REFRESH_SECONDS", 300.0)

        out = kg_conn.get_neo4j_graph()

        assert out is graph
        graph.refresh_schema.assert_called_once()

    def test_ttl_refresh_failure_keeps_old_schema(self, _isolate_singleton, monkeypatch):
        kg_conn = _isolate_singleton
        graph = self._prime_cache(kg_conn, monkeypatch, refreshed_age=10_000.0)
        graph.refresh_schema.side_effect = RuntimeError("connection reset")
        monkeypatch.setattr(kg_conn.settings, "NEO4J_SCHEMA_REFRESH_SECONDS", 300.0)

        # 刷新失败绝不打挂请求：照常返回缓存实例（旧 schema）
        out = kg_conn.get_neo4j_graph()
        assert out is graph

        # 时间戳已推进 → 立即再次取用不重试（失败不许打成风暴）
        kg_conn.get_neo4j_graph()
        assert graph.refresh_schema.call_count == 1

    def test_nonpositive_ttl_keeps_legacy_single_refresh(
        self, _isolate_singleton, monkeypatch
    ):
        kg_conn = _isolate_singleton
        graph = self._prime_cache(kg_conn, monkeypatch, refreshed_age=10**9)
        monkeypatch.setattr(kg_conn.settings, "NEO4J_SCHEMA_REFRESH_SECONDS", 0.0)

        kg_conn.get_neo4j_graph()

        graph.refresh_schema.assert_not_called()

    def test_reset_clears_cache_and_refresh_timestamp(self, _isolate_singleton):
        kg_conn = _isolate_singleton
        kg_conn.reset_neo4j_graph()
        assert kg_conn._graph_cache is None
        assert kg_conn._schema_refreshed_at is None

    def test_settings_defaults_are_pinned(self):
        from agentix.config import settings

        assert settings.NEO4J_SCHEMA_REFRESH_SECONDS == 300.0
        assert settings.NEO4J_QUERY_TIMEOUT_SECONDS == 0.0


class TestCypherReadonlyGateAndTimeout:
    """执行缝只读闸 fail-closed（不触库）+ NEO4J_QUERY_TIMEOUT_SECONDS 可选超时。"""

    @staticmethod
    def _conn_module():
        import agentix.application.agents.kg_sub_graph.kg_neo4j_conn as kg_conn

        return kg_conn

    @pytest.mark.parametrize(
        "statement",
        [
            "MATCH (n) SET n.x = 1 RETURN n",
            "CREATE (n:Fake {x: 1}) RETURN n",
            "MATCH (n) WHERE n.x = 1 DELETE n",
            "CALL apoc.periodic.iterate('MATCH (n) RETURN n', 'SET n.x = 1', {})",
            "LOAD CSV WITH HEADERS FROM 'file:///x.csv' AS row RETURN row",
        ],
    )
    def test_write_clause_fails_closed_before_touching_db(self, statement):
        kg_conn = self._conn_module()
        graph = MagicMock()

        with pytest.raises(ValueError, match="write clause"):
            kg_conn.query_readonly(graph, statement)

        graph.query.assert_not_called()

    def test_read_query_keeps_baseline_call_shape(self):
        kg_conn = self._conn_module()
        graph = MagicMock()
        graph.query.return_value = [{"n": 1}]

        out = kg_conn.query_readonly(graph, "MATCH (n) RETURN n")

        assert out == [{"n": 1}]
        assert not inspect.isawaitable(out)  # timeout=0 → 同步内联，非协程
        graph.query.assert_called_once_with("MATCH (n) RETURN n")

    def test_param_query_keeps_keyword_call_shape(self):
        kg_conn = self._conn_module()
        graph = MagicMock()
        graph.query.return_value = []

        kg_conn.query_readonly(graph, "MATCH (n) WHERE n.x = $x RETURN n", {"x": 1})

        graph.query.assert_called_once_with(
            query="MATCH (n) WHERE n.x = $x RETURN n", params={"x": 1}
        )

    @pytest.mark.asyncio
    async def test_timeout_enforced_when_configured(self, monkeypatch):
        kg_conn = self._conn_module()
        graph = MagicMock()
        graph.query.side_effect = lambda *a, **k: time.sleep(0.3)
        monkeypatch.setattr(kg_conn.settings, "NEO4J_QUERY_TIMEOUT_SECONDS", 0.05)

        with pytest.raises(RuntimeError, match="NEO4J_QUERY_TIMEOUT_SECONDS"):
            await kg_conn.query_readonly(graph, "MATCH (n) RETURN n")

    def test_all_execution_seams_route_through_query_readonly(self):
        seams = {
            "agentix/application/agents/kg_sub_graph/agentic_rag_agents/"
            "components/text2cypher/execution/node.py": 1,
            "agentix/application/agents/kg_sub_graph/agentic_rag_agents/"
            "components/predefined_cypher/node.py": 1,
            "agentix/application/agents/kg_sub_graph/agentic_rag_agents/"
            "components/cypher_tools/utils.py": 1,
            "agentix/application/services/kg_gateway_bridge.py": 2,
        }
        for rel, min_calls in seams.items():
            src = (REPO_ROOT / rel).read_text(encoding="utf-8")
            assert src.count("query_readonly(") >= min_calls, f"{rel} 未接执行缝"

    def test_bridge_handlers_no_longer_touch_graph_query_directly(self):
        src = (
            REPO_ROOT / "agentix/application/services/kg_gateway_bridge.py"
        ).read_text(encoding="utf-8")
        assert "_neo4j_graph().query" not in src

    def test_hard_gate_covers_predefined_templates(self):
        """预定义模板全量过只读闸——模板一旦引入写子句必须当场响亮失败。"""
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.predefined_cypher.legal_cypher_dict import (
            predefined_cypher_dict,
        )

        kg_conn = self._conn_module()
        for name, statement in predefined_cypher_dict.items():
            try:
                kg_conn.assert_readonly_cypher(statement)
            except ValueError as exc:
                raise AssertionError(f"预定义模板 {name} 含写子句: {exc}") from exc


class TestDynamicExampleClassificationDegraded:
    """dynamic retriever 显式未实现——不再静默关键词分类，降级走通用示例。"""

    @staticmethod
    def _retriever_cls():
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.retrievers.cypher_examples.legal_retriever import (
            LegalCypherRetriever,
        )

        return LegalCypherRetriever

    def test_dynamic_classify_is_explicitly_unimplemented(self):
        cls = self._retriever_cls()
        with pytest.raises(NotImplementedError, match="R-P0-4"):
            cls._dynamic_classify("合同违约相关法条有哪些")

    def test_get_examples_degrades_to_general_examples_with_warning(self, caplog):
        cls = self._retriever_cls()
        with caplog.at_level(logging.WARNING, logger=cls.__module__):
            out = cls().get_examples("盗窃相关的法律条文", k=3)

        # 降级不失败：通用示例照常返回
        assert "Question:" in out
        assert "Cypher:" in out

        messages = [r.getMessage() for r in caplog.records]
        assert any("R-P0-4" in m for m in messages), messages
        assert any("falling back to general examples" in m for m in messages), messages

    def test_silent_keyword_classification_is_gone(self):
        src = (
            REPO_ROOT
            / "agentix/application/agents/kg_sub_graph/agentic_rag_agents/"
            "retrievers/cypher_examples/legal_retriever.py"
        ).read_text(encoding="utf-8")
        # 旧静默分类器与类型桶不许复活
        assert "def _classify(" not in src
        assert "type_based_examples" not in src


# ---------------------------------------------------------------------------
# R-P0-5：打点不吞错（失败显式透出 + 主链路完好 + finalize 证据保留）
# ---------------------------------------------------------------------------


class TestTelemetryDegradedSurfacing:
    """R-P0-5：``record_rag_retrieval``/``record_retrieval`` 失败必须显式透出。"""

    @staticmethod
    def _kb_module():
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent import (
            kb_retrieval,
        )

        return kb_retrieval

    @staticmethod
    def _capture_degraded(monkeypatch) -> list:
        """把 ``record_degraded`` 换成捕获器（不依赖 Prometheus 抓取面顺序）。"""
        import agentix.observability.metrics as metrics_module

        captured: list = []
        monkeypatch.setattr(
            metrics_module, "record_degraded", lambda component: captured.append(component)
        )
        return captured

    @pytest.mark.asyncio
    async def test_record_failure_returns_false_and_marks_degraded_counter(
        self, monkeypatch
    ):
        import agentix.application.services.observability as obs_module

        def _boom():
            raise RuntimeError("redis down")

        monkeypatch.setattr(obs_module, "get_observability", _boom)
        captured = self._capture_degraded(monkeypatch)
        kb = self._kb_module()

        ok = await kb._record_rag_retrieval("postgres", 1, 2.5)

        assert ok is False
        assert "rag_retrieval_postgres" in captured

    @pytest.mark.asyncio
    async def test_record_failure_emits_warning_and_survives_counter_failure(
        self, monkeypatch
    ):
        """warning 留痕为契约；连 ``record_degraded`` 都失败时仍不许二次抛出。"""
        import agentix.application.services.observability as obs_module
        import agentix.observability.metrics as metrics_module

        def _boom():
            raise RuntimeError("redis down")

        monkeypatch.setattr(obs_module, "get_observability", _boom)
        monkeypatch.setattr(
            metrics_module,
            "record_degraded",
            lambda component: (_ for _ in ()).throw(RuntimeError("metrics down")),
        )
        kb = self._kb_module()

        logs: list = []
        sink_id = kb.kb_logger.add(lambda message: logs.append(str(message)))
        try:
            ok = await kb._record_rag_retrieval("milvus", 0, 0.0)
        finally:
            kb.kb_logger.remove(sink_id)

        assert ok is False
        assert any("telemetry degraded (R-P0-5" in line for line in logs), logs

    @pytest.mark.asyncio
    async def test_record_success_returns_true_without_degraded(self, monkeypatch):
        import agentix.application.services.observability as obs_module

        svc = SimpleNamespace(record_rag_retrieval=AsyncMock())
        monkeypatch.setattr(obs_module, "get_observability", lambda: svc)
        captured = self._capture_degraded(monkeypatch)
        kb = self._kb_module()

        ok = await kb._record_rag_retrieval("milvus", 2, 1.0)

        assert ok is True
        svc.record_rag_retrieval.assert_awaited_once_with(
            source="milvus", result_count=2, latency_ms=1.0
        )
        assert captured == []

    @pytest.mark.asyncio
    async def test_chat_retrieval_telemetry_failure_does_not_break_retrieval(
        self, monkeypatch
    ):
        """chat 侧 ``record_retrieval`` 抛出时，已成功的检索结果照常返回。"""
        import agentix.observability as obs_pkg
        from agentix.retrieval.usecases import chat_service

        def _boom(**_kwargs):
            raise RuntimeError("prometheus down")

        monkeypatch.setattr(obs_pkg, "record_retrieval", _boom)
        captured: list = []
        monkeypatch.setattr(obs_pkg, "record_degraded", lambda c: captured.append(c))
        monkeypatch.setattr(
            chat_service,
            "retrieve_docs_with_retry",
            lambda *a, **k: [{"page_content": "法条原文", "metadata": {}}],
        )

        out = await chat_service.retrieve_scoped_docs_cached(
            kb_id="kb1",
            question="合同违约责任",
            search_k=5,
            metadata_filter=None,
            vectorstore=object(),
            cache_redis=None,
            artifact_id="art1",
            scope_token="tok",
        )

        assert out and out[0]["page_content"] == "法条原文"
        assert "chat_retrieval" in captured


class TestFinalizeDegradeEvidence:
    """R-P0-5 回归：finalize 保留通道 ``degraded`` 位与双通道原文（P1 融合证据）。"""

    _CFG = {"configurable": {}}

    @staticmethod
    def _finalize_node():
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_nodes import (
            create_finalize_node,
        )

        llm = MagicMock()
        final_response = MagicMock()
        final_response.content = "合成回答"
        llm.ainvoke = AsyncMock(return_value=final_response)
        return llm, create_finalize_node(llm, top_k=3)

    @pytest.mark.asyncio
    async def test_dual_channel_contexts_and_degraded_passthrough(self):
        llm, node = self._finalize_node()
        state = {
            "guardrails_decision": "proceed",
            "question": "合同违约责任",
            "degraded": True,
            "milvus_results": [
                {"content": "释义原文", "tool": "milvus", "metadata": {"title": "释义"}}
            ],
            "postgres_results": [
                {"content": "法条原文", "tool": "postgres", "metadata": {"title": "法条"}}
            ],
            "local_results": [
                {"content": "释义原文", "tool": "milvus", "metadata": {"title": "释义"}},
                {"content": "法条原文", "tool": "postgres", "metadata": {"title": "法条"}},
            ],
            "external_results": [],
        }

        result = await node(state, config=self._CFG)

        # 通道 degraded 位原样透出（finalize 不吞不改）
        assert result["degraded"] is True
        # 双通道原文都进了最终 prompt（为 P1 融合留证据）
        messages = llm.ainvoke.await_args.args[0]
        prompt_text = "\n".join(str(m.content) for m in messages)
        assert "释义原文" in prompt_text
        assert "法条原文" in prompt_text
        # 双通道 sources 都保留
        assert {"释义", "法条"} <= set(result["sources"])

    @pytest.mark.asyncio
    async def test_healthy_state_passes_degraded_false(self):
        _, node = self._finalize_node()
        state = {
            "guardrails_decision": "proceed",
            "question": "q",
            "milvus_results": [],
            "postgres_results": [
                {"content": "法条原文", "tool": "postgres", "metadata": {"title": "法条"}}
            ],
            "local_results": [
                {"content": "法条原文", "tool": "postgres", "metadata": {"title": "法条"}}
            ],
            "external_results": [],
        }

        result = await node(state, config=self._CFG)

        assert result["degraded"] is False

    @pytest.mark.asyncio
    async def test_guardrails_end_resets_degraded_intentionally(self):
        """guardrails 拒答是有意终态：不计 degraded（既有语义，钉死不回归）。"""
        llm, node = self._finalize_node()
        state = {
            "guardrails_decision": "end",
            "summary": "不可回答",
            "degraded": True,
        }

        result = await node(state, config=self._CFG)

        assert result["degraded"] is False
        assert result["answer"] == "不可回答"
        llm.ainvoke.assert_not_awaited()
