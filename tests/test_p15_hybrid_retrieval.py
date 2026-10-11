"""P1-5 检索并发化验收测试。

方案验收标准：
1. 并发：两路都被选中时耗时从 T_pg + T_milvus 降至 max(T_pg, T_milvus)
2. 部分成功：单路异常不拖垮整体，degraded=True
3. 全失败：抛 AgentError(RETRIEVAL_FAILED)
4. 空结果 ≠ 失败：成功返回空列表不算失败
5. 单路选择：只选 postgres 时 milvus 通道不被调用
6. postgres url 缺失但选了 postgres → 自动走 milvus（现行为保留）

全部用例 mock 外部依赖（KnowledgeService / aiohttp POST），不发真实请求。
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent import (
    kb_retrieval,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_nodes import (
    local_search_node as local_search_module,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_nodes import (
    create_local_search_node,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_retrieval import (
    hybrid_retrieve,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_states import (
    KBWorkflowConfig,
)
from agentix.domain.errors import AgentError, ErrorCode


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


def _make_ks():
    ks = MagicMock()
    ks.reranker.enabled = False
    ks.search = AsyncMock(return_value=[])
    return ks


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. 并发：两路都 want → 两路都被调用且并发执行
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_both_channels_called_concurrently(self, monkeypatch):
        cfg = _make_cfg(postgres_search_url="http://kb/api/v1/knowledge/search")
        ks = _make_ks()
        calls = {"postgres": 0, "milvus": 0}

        async def fake_pg(question, *, cfg, knowledge_service):
            calls["postgres"] += 1
            await asyncio.sleep(0.15)
            return [{"content": "pg", "tool": "postgres", "metadata": {}}]

        async def fake_milvus(question, *, cfg, knowledge_service):
            calls["milvus"] += 1
            await asyncio.sleep(0.15)
            return [{"content": "milvus", "tool": "milvus", "metadata": {}}]

        monkeypatch.setattr(kb_retrieval, "_postgres_search", fake_pg)
        monkeypatch.setattr(kb_retrieval, "_milvus_search", fake_milvus)

        start = time.monotonic()
        out = _run(
            hybrid_retrieve("q", cfg=cfg, knowledge_service=ks, tools=["postgres", "milvus"])
        )
        elapsed = time.monotonic() - start

        # 两路均被调用
        assert calls == {"postgres": 1, "milvus": 1}
        # 并发而非串行：串行约 0.30s，并发约 0.15s（阈值放宽避免 flaky）
        assert elapsed < 0.28
        assert out["postgres"][0]["tool"] == "postgres"
        assert out["milvus"][0]["tool"] == "milvus"
        assert out["degraded"] is False
        assert out["failures"] == []


# ---------------------------------------------------------------------------
# 2. 部分成功：单路失败不拖垮整体
# ---------------------------------------------------------------------------

class TestPartialFailure:
    def test_milvus_failure_keeps_postgres(self, monkeypatch):
        cfg = _make_cfg(postgres_search_url="http://kb/api/v1/knowledge/search")
        ks = _make_ks()

        async def fake_pg(question, *, cfg, knowledge_service):
            return [{"content": "pg", "tool": "postgres", "metadata": {}}]

        async def fake_milvus(question, *, cfg, knowledge_service):
            raise RuntimeError("milvus down")

        monkeypatch.setattr(kb_retrieval, "_postgres_search", fake_pg)
        monkeypatch.setattr(kb_retrieval, "_milvus_search", fake_milvus)

        out = _run(
            hybrid_retrieve("q", cfg=cfg, knowledge_service=ks, tools=["postgres", "milvus"])
        )
        assert out["failures"] == ["milvus"]
        assert out["degraded"] is True
        assert out["postgres"][0]["tool"] == "postgres"
        assert out["milvus"] == []

    def test_postgres_failure_keeps_milvus(self, monkeypatch):
        cfg = _make_cfg(postgres_search_url="http://kb/api/v1/knowledge/search")
        ks = _make_ks()

        async def fake_pg(question, *, cfg, knowledge_service):
            raise RuntimeError("pg down")

        async def fake_milvus(question, *, cfg, knowledge_service):
            return [{"content": "milvus", "tool": "milvus", "metadata": {}}]

        monkeypatch.setattr(kb_retrieval, "_postgres_search", fake_pg)
        monkeypatch.setattr(kb_retrieval, "_milvus_search", fake_milvus)

        out = _run(
            hybrid_retrieve("q", cfg=cfg, knowledge_service=ks, tools=["postgres", "milvus"])
        )
        assert out["failures"] == ["postgres"]
        assert out["degraded"] is True
        assert out["postgres"] == []
        assert out["milvus"][0]["tool"] == "milvus"


# ---------------------------------------------------------------------------
# 3. 全失败：抛 AgentError(RETRIEVAL_FAILED)
# ---------------------------------------------------------------------------

class TestAllFailed:
    def test_both_channels_failed_raises_retrieval_failed(self, monkeypatch):
        cfg = _make_cfg(postgres_search_url="http://kb/api/v1/knowledge/search")
        ks = _make_ks()

        async def fake_pg(question, *, cfg, knowledge_service):
            raise RuntimeError("pg down")

        async def fake_milvus(question, *, cfg, knowledge_service):
            raise RuntimeError("milvus down")

        monkeypatch.setattr(kb_retrieval, "_postgres_search", fake_pg)
        monkeypatch.setattr(kb_retrieval, "_milvus_search", fake_milvus)

        with pytest.raises(AgentError) as excinfo:
            _run(
                hybrid_retrieve(
                    "q", cfg=cfg, knowledge_service=ks, tools=["postgres", "milvus"]
                )
            )
        assert excinfo.value.code is ErrorCode.RETRIEVAL_FAILED

    def test_single_selected_channel_failed_raises(self, monkeypatch):
        cfg = _make_cfg(postgres_search_url="http://kb/api/v1/knowledge/search")
        ks = _make_ks()

        async def fake_pg(question, *, cfg, knowledge_service):
            raise RuntimeError("pg down")

        monkeypatch.setattr(kb_retrieval, "_postgres_search", fake_pg)

        with pytest.raises(AgentError) as excinfo:
            _run(hybrid_retrieve("q", cfg=cfg, knowledge_service=ks, tools=["postgres"]))
        assert excinfo.value.code is ErrorCode.RETRIEVAL_FAILED


# ---------------------------------------------------------------------------
# 4. 空结果 ≠ 失败
# ---------------------------------------------------------------------------

class TestEmptyIsNotFailure:
    def test_empty_results_do_not_raise(self, monkeypatch):
        cfg = _make_cfg(postgres_search_url="http://kb/api/v1/knowledge/search")
        ks = _make_ks()

        async def fake_pg(question, *, cfg, knowledge_service):
            return []

        async def fake_milvus(question, *, cfg, knowledge_service):
            return []

        monkeypatch.setattr(kb_retrieval, "_postgres_search", fake_pg)
        monkeypatch.setattr(kb_retrieval, "_milvus_search", fake_milvus)

        out = _run(
            hybrid_retrieve("q", cfg=cfg, knowledge_service=ks, tools=["postgres", "milvus"])
        )
        assert out["postgres"] == []
        assert out["milvus"] == []
        assert out["failures"] == []
        assert out["degraded"] is False


# ---------------------------------------------------------------------------
# 5. 单路选择：未选中的通道不被调用
# ---------------------------------------------------------------------------

class TestChannelSelection:
    def test_only_postgres_selected_milvus_not_called(self, monkeypatch):
        cfg = _make_cfg(postgres_search_url="http://kb/api/v1/knowledge/search")
        ks = _make_ks()
        calls = {"postgres": 0, "milvus": 0}

        async def fake_pg(question, *, cfg, knowledge_service):
            calls["postgres"] += 1
            return [{"content": "pg", "tool": "postgres", "metadata": {}}]

        async def fake_milvus(question, *, cfg, knowledge_service):
            calls["milvus"] += 1
            return []

        monkeypatch.setattr(kb_retrieval, "_postgres_search", fake_pg)
        monkeypatch.setattr(kb_retrieval, "_milvus_search", fake_milvus)

        out = _run(hybrid_retrieve("q", cfg=cfg, knowledge_service=ks, tools=["postgres"]))
        assert calls == {"postgres": 1, "milvus": 0}
        assert out["milvus"] == []
        assert out["degraded"] is False

    def test_only_milvus_selected_postgres_not_called(self, monkeypatch):
        cfg = _make_cfg(postgres_search_url="http://kb/api/v1/knowledge/search")
        ks = _make_ks()
        calls = {"postgres": 0, "milvus": 0}

        async def fake_pg(question, *, cfg, knowledge_service):
            calls["postgres"] += 1
            return []

        async def fake_milvus(question, *, cfg, knowledge_service):
            calls["milvus"] += 1
            return [{"content": "m", "tool": "milvus", "metadata": {}}]

        monkeypatch.setattr(kb_retrieval, "_postgres_search", fake_pg)
        monkeypatch.setattr(kb_retrieval, "_milvus_search", fake_milvus)

        out = _run(hybrid_retrieve("q", cfg=cfg, knowledge_service=ks, tools=["milvus"]))
        assert calls == {"postgres": 0, "milvus": 1}
        assert out["milvus"][0]["tool"] == "milvus"

    def test_no_tools_returns_empty_without_calls(self, monkeypatch):
        cfg = _make_cfg(postgres_search_url="http://kb/api/v1/knowledge/search")
        ks = _make_ks()
        calls = {"postgres": 0, "milvus": 0}

        async def fake_pg(question, *, cfg, knowledge_service):
            calls["postgres"] += 1
            return []

        async def fake_milvus(question, *, cfg, knowledge_service):
            calls["milvus"] += 1
            return []

        monkeypatch.setattr(kb_retrieval, "_postgres_search", fake_pg)
        monkeypatch.setattr(kb_retrieval, "_milvus_search", fake_milvus)

        out = _run(hybrid_retrieve("q", cfg=cfg, knowledge_service=ks, tools=[]))
        assert calls == {"postgres": 0, "milvus": 0}
        assert out == {"postgres": [], "milvus": [], "failures": [], "degraded": False}


# ---------------------------------------------------------------------------
# 6. postgres url 缺失但选了 postgres → 自动走 milvus（现行为保留）
# ---------------------------------------------------------------------------

class TestPostgresUrlMissingFallback:
    def test_falls_back_to_milvus(self, monkeypatch):
        cfg = _make_cfg(postgres_search_url=None)
        ks = _make_ks()
        calls = {"postgres": 0, "milvus": 0}

        async def fake_pg(question, *, cfg, knowledge_service):
            calls["postgres"] += 1
            return []

        async def fake_milvus(question, *, cfg, knowledge_service):
            calls["milvus"] += 1
            return [{"content": "m", "tool": "milvus", "metadata": {}}]

        monkeypatch.setattr(kb_retrieval, "_postgres_search", fake_pg)
        monkeypatch.setattr(kb_retrieval, "_milvus_search", fake_milvus)

        out = _run(hybrid_retrieve("q", cfg=cfg, knowledge_service=ks, tools=["postgres"]))
        assert calls == {"postgres": 0, "milvus": 1}
        assert out["milvus"][0]["tool"] == "milvus"
        assert out["degraded"] is False


# ---------------------------------------------------------------------------
# 7. _postgres_search 内部：POST 到 kb_ingest 用 mock（不发真实请求）
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    async def text(self):
        return str(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    def __init__(self, response):
        self._response = response
        self.posted = []
        # 2026-09：出站调用现在会带 ``headers``（kb_ingest 开启鉴权后必须带
        # ``X-API-Key``）。单列一个列表而不是把 ``posted`` 改成三元组——
        # 那会破坏既有 `url, payload = session.posted[0]` 的解包。
        self.posted_headers = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, url, json=None, headers=None):
        self.posted.append((url, json))
        self.posted_headers.append(headers)
        return self._response


class _FailingSession:
    """模拟 kb_ingest 服务未启动：post 直接抛传输层异常。"""

    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, url, json=None, headers=None):
        raise self._exc


class TestPostgresSearchInternal:
    def test_payload_and_result_mapping(self, monkeypatch):
        cfg = _make_cfg(postgres_search_url="http://kb/api/v1/knowledge/search")
        ks = _make_ks()
        response = _FakeResponse(
            200,
            {
                "results": [
                    {"content": "c1", "metadata": {"title": "T"}, "similarity": 0.95},
                    {"content": "c2", "score": 0.9},
                ]
            },
        )
        session = _FakeSession(response)
        monkeypatch.setattr(kb_retrieval.aiohttp, "ClientSession", lambda *a, **k: session)

        out = _run(kb_retrieval._postgres_search("民法典", cfg=cfg, knowledge_service=ks))

        # POST 到配置的 kb_ingest 地址，payload 携带 query/top_k/threshold
        assert len(session.posted) == 1
        url, payload = session.posted[0]
        assert url == "http://kb/api/v1/knowledge/search"
        assert payload["query"] == "民法典"
        assert payload["top_k"] == cfg.top_k

        # 结果映射：tool=postgres、metadata 独立副本、id 兜底、similarity 归一
        assert out[0]["tool"] == "postgres"
        assert out[0]["id"] == "postgres_0"
        assert out[0]["metadata"] == {"title": "T"}
        assert out[1]["id"] == "postgres_1"
        assert out[1]["similarity"] == 0.9

    def test_non_200_raises_channel_failure(self, monkeypatch):
        """HTTP 非 200 → 视为通道失败并向上抛（服务活着但报错，区别于「查不到」）。"""
        cfg = _make_cfg(postgres_search_url="http://kb/api/v1/knowledge/search")
        ks = _make_ks()
        response = _FakeResponse(500, "boom")
        session = _FakeSession(response)
        monkeypatch.setattr(kb_retrieval.aiohttp, "ClientSession", lambda *a, **k: session)

        with pytest.raises(RuntimeError, match="HTTP 500"):
            _run(kb_retrieval._postgres_search("q", cfg=cfg, knowledge_service=ks))

    def test_transport_error_raises(self, monkeypatch):
        """连接被拒（kb_ingest 未启动）→ 向上抛，不再吞掉当「空结果」。"""
        cfg = _make_cfg(postgres_search_url="http://kb/api/v1/knowledge/search")
        ks = _make_ks()
        monkeypatch.setattr(
            kb_retrieval.aiohttp,
            "ClientSession",
            lambda *a, **k: _FailingSession(aiohttp.ClientError("connection refused")),
        )

        with pytest.raises(aiohttp.ClientError):
            _run(kb_retrieval._postgres_search("q", cfg=cfg, knowledge_service=ks))

    def test_200_bad_structure_returns_empty_not_failure(self, monkeypatch):
        """服务正常（200）但 results 结构不符 → 空结果，不算通道失败。"""
        cfg = _make_cfg(postgres_search_url="http://kb/api/v1/knowledge/search")
        ks = _make_ks()
        response = _FakeResponse(200, {"results": "not-a-list"})
        session = _FakeSession(response)
        monkeypatch.setattr(kb_retrieval.aiohttp, "ClientSession", lambda *a, **k: session)

        out = _run(kb_retrieval._postgres_search("q", cfg=cfg, knowledge_service=ks))
        assert out == []


# ---------------------------------------------------------------------------
# 7b. 服务故障 vs 无结果：验收 3 的端到端证明（真实 _postgres_search + mock 传输层）
# ---------------------------------------------------------------------------


class TestChannelFailureSemantics:
    def _patch_pg_transport_down(self, monkeypatch):
        monkeypatch.setattr(
            kb_retrieval.aiohttp,
            "ClientSession",
            lambda *a, **k: _FailingSession(aiohttp.ClientError("connection refused")),
        )

    def test_postgres_stopped_and_milvus_down_raises_retrieval_failed(self, monkeypatch):
        """验收 3：两路都停 → AgentError(RETRIEVAL_FAILED)（原实现永远触发不了）。"""
        cfg = _make_cfg(postgres_search_url="http://kb/api/v1/knowledge/search")
        ks = _make_ks()
        self._patch_pg_transport_down(monkeypatch)

        async def fake_milvus(question, *, cfg, knowledge_service):
            raise RuntimeError("milvus down")

        monkeypatch.setattr(kb_retrieval, "_milvus_search", fake_milvus)

        with pytest.raises(AgentError) as excinfo:
            _run(
                hybrid_retrieve(
                    "q", cfg=cfg, knowledge_service=ks, tools=["postgres", "milvus"]
                )
            )
        assert excinfo.value.code is ErrorCode.RETRIEVAL_FAILED

    def test_postgres_stopped_milvus_ok_degrades(self, monkeypatch):
        """验收 2：postgres 服务停掉 → 记通道失败 + degraded=True，milvus 结果保留。"""
        cfg = _make_cfg(postgres_search_url="http://kb/api/v1/knowledge/search")
        ks = _make_ks()
        self._patch_pg_transport_down(monkeypatch)

        async def fake_milvus(question, *, cfg, knowledge_service):
            return [{"content": "m", "tool": "milvus", "metadata": {}}]

        monkeypatch.setattr(kb_retrieval, "_milvus_search", fake_milvus)

        out = _run(
            hybrid_retrieve(
                "q", cfg=cfg, knowledge_service=ks, tools=["postgres", "milvus"]
            )
        )
        assert out["failures"] == ["postgres"]
        assert out["degraded"] is True
        assert out["postgres"] == []
        assert out["milvus"][0]["tool"] == "milvus"

    def test_postgres_http500_and_milvus_down_raises(self, monkeypatch):
        """HTTP 500 属通道失败：与 milvus 异常叠加 → RETRIEVAL_FAILED。"""
        cfg = _make_cfg(postgres_search_url="http://kb/api/v1/knowledge/search")
        ks = _make_ks()
        session = _FakeSession(_FakeResponse(500, "boom"))
        monkeypatch.setattr(kb_retrieval.aiohttp, "ClientSession", lambda *a, **k: session)

        async def fake_milvus(question, *, cfg, knowledge_service):
            raise RuntimeError("milvus down")

        monkeypatch.setattr(kb_retrieval, "_milvus_search", fake_milvus)

        with pytest.raises(AgentError) as excinfo:
            _run(
                hybrid_retrieve(
                    "q", cfg=cfg, knowledge_service=ks, tools=["postgres", "milvus"]
                )
            )
        assert excinfo.value.code is ErrorCode.RETRIEVAL_FAILED

    def test_postgres_only_selected_and_stopped_raises(self, monkeypatch):
        """只选 postgres 且服务停掉 → 单通道全失败，同样抛 RETRIEVAL_FAILED。"""
        cfg = _make_cfg(postgres_search_url="http://kb/api/v1/knowledge/search")
        ks = _make_ks()
        self._patch_pg_transport_down(monkeypatch)

        with pytest.raises(AgentError) as excinfo:
            _run(hybrid_retrieve("q", cfg=cfg, knowledge_service=ks, tools=["postgres"]))
        assert excinfo.value.code is ErrorCode.RETRIEVAL_FAILED

    def test_pg_200_bad_structure_not_counted_as_failure(self, monkeypatch):
        """200 但结构不符 → 通道不算失败（degraded=False、failures 空）。"""
        cfg = _make_cfg(postgres_search_url="http://kb/api/v1/knowledge/search")
        ks = _make_ks()
        session = _FakeSession(_FakeResponse(200, {"results": "not-a-list"}))
        monkeypatch.setattr(kb_retrieval.aiohttp, "ClientSession", lambda *a, **k: session)

        async def fake_milvus(question, *, cfg, knowledge_service):
            return []

        monkeypatch.setattr(kb_retrieval, "_milvus_search", fake_milvus)

        out = _run(
            hybrid_retrieve(
                "q", cfg=cfg, knowledge_service=ks, tools=["postgres", "milvus"]
            )
        )
        assert out["failures"] == []
        assert out["degraded"] is False


# ---------------------------------------------------------------------------
# 8. local_search 节点层：degraded 透传 + postgres 优先
# ---------------------------------------------------------------------------

class TestLocalSearchNodeDegraded:
    def test_empty_question_short_circuit_has_degraded_false(self):
        node = create_local_search_node(_make_ks(), _make_cfg())
        result = _run(node({"question": "   "}))
        assert result["milvus_results"] == []
        assert result["postgres_results"] == []
        assert result["local_results"] == []
        assert result["degraded"] is False

    def test_node_surfaces_degraded_and_postgres_priority(self, monkeypatch):
        cfg = _make_cfg()
        ks = _make_ks()

        async def fake_hybrid(question, *, cfg, knowledge_service, tools):
            return {
                "postgres": [{"content": "pg", "tool": "postgres", "metadata": {}}],
                "milvus": [{"content": "m", "tool": "milvus", "metadata": {}}],
                "failures": ["milvus"],
                "degraded": True,
            }

        monkeypatch.setattr(local_search_module, "hybrid_retrieve", fake_hybrid)

        node = create_local_search_node(ks, cfg)
        result = _run(node({"question": "q", "kb_tools": ["postgres", "milvus"]}))

        assert result["degraded"] is True
        # 两路结果都保留（都会被 finalize 塞进 prompt 上下文）
        assert result["postgres_results"][0]["tool"] == "postgres"
        assert result["milvus_results"][0]["tool"] == "milvus"
        # local_results 保持 postgres 优先
        assert result["local_results"] == result["postgres_results"]

    def test_node_local_results_falls_back_to_milvus(self, monkeypatch):
        cfg = _make_cfg()
        ks = _make_ks()

        async def fake_hybrid(question, *, cfg, knowledge_service, tools):
            return {
                "postgres": [],
                "milvus": [{"content": "m", "tool": "milvus", "metadata": {}}],
                "failures": [],
                "degraded": False,
            }

        monkeypatch.setattr(local_search_module, "hybrid_retrieve", fake_hybrid)

        node = create_local_search_node(ks, cfg)
        result = _run(node({"question": "q", "kb_tools": ["postgres", "milvus"]}))

        assert result["postgres_results"] == []
        assert result["local_results"] == result["milvus_results"]
        assert result["degraded"] is False
