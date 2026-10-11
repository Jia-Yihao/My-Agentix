"""P0-2 收口验收测试：补齐三处「写了但没接线」的接线证明（全离线）。

覆盖（对应 5 项改动 + 8 条必测）：
1. **超时**：``AGENT_TIMEOUT`` 经 ``asyncio.wait_for`` 包住 ``graph.ainvoke`` 真正生效
2. **recursion_limit**：``ask()`` / ``stream_events()`` 两条路径的 config **顶层键**
   == ``settings.MAX_ITERATIONS``
3. **错误分类**：``ErrorCode.AGENT_TIMEOUT in DEGRADABLE`` 且 ``HTTP_FATAL == 504``
4. **重试成功**：``_postgres_search`` 首次传输失败、二次 200 → 正常返回结果
5. **重试耗尽仍算通道失败**：``hybrid_retrieve`` 记入 ``failures``；两路耗尽抛
   ``AgentError(RETRIEVAL_FAILED)``（P1-5 语义不变）
6. **角色接线**：router / hallucination / summarizer / general 四处调用含 ``role=``；
   语义无歧义外的 4 处保持未改
7. **埋点不影响主链路且不吞错**：``get_observability`` 抛异常 → 检索照常返回，
   且失败显式透出为 ``record_degraded`` 计数 + ``_record_rag_retrieval`` 返回
   ``False``（R-P0-5，不再 debug 吞掉）
8. **流式墙钟守卫**：极小 ``AGENT_TIMEOUT`` + 慢事件流 → ``error`` 事件 + ``done`` 恰好一次

全部 mock / 假 graph，不依赖 LLM / Redis / Neo4j / Milvus。
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import aiohttp
import pytest
from fastapi import HTTPException
from langchain_core.messages import AIMessage

from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent import (
    kb_retrieval,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_retrieval import (
    hybrid_retrieve,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_states import (
    KBWorkflowConfig,
)
from agentix.application.services.chat_service import ChatService
from agentix.config import settings as settings_instance
from agentix.domain.errors import DEGRADABLE, HTTP_FATAL, AgentError, ErrorCode

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def _run(coro):
    """在独立事件循环里跑一个协程（与 test_p15 同风格，避免 marker 依赖）。"""
    return asyncio.run(coro)


def _collect(agen):
    """把异步生成器收集成 list。"""

    async def _run_all():
        return [event async for event in agen]

    return asyncio.run(_run_all())


def _patch_obs(monkeypatch):
    """把 observability.get_observability 换成 AsyncMock（避免连 Redis）。"""
    import agentix.application.services.observability as obs_module

    svc = AsyncMock()
    monkeypatch.setattr(obs_module, "get_observability", lambda: svc)
    return svc


def _no_sleep(monkeypatch):
    """把 ``asyncio.sleep`` 换成 no-op，加速退避重试（aretry 引用同一模块对象）。"""

    async def _immediate(_delay, *args, **kwargs):
        return None

    monkeypatch.setattr(asyncio, "sleep", _immediate)


def _ok_result():
    return {
        "messages": [AIMessage(content="答复内容")],
        "router": {"type": "kb-query", "logic": "test"},
        "sources": [],
    }


class _Chunk:
    """最小消息块替身（仅需 ``content``）。"""

    def __init__(self, content):
        self.content = content


def _ev(kind, name=None, data=None, metadata=None):
    return {"event": kind, "name": name, "data": data or {}, "metadata": metadata or {}}


# ---------------------------------------------------------------------------
# 假 graph
# ---------------------------------------------------------------------------


class _CapturingGraph:
    """记录传给 ``ainvoke`` 的 config；可注入延迟/异常。"""

    def __init__(self, delay=0.0, result=None, exc=None):
        self.delay = delay
        self.result = result if result is not None else _ok_result()
        self.exc = exc
        self.captured_config = None
        self.captured_input = None

    async def ainvoke(self, input_state, config=None):
        self.captured_input = input_state
        self.captured_config = config
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return self.result


class _StreamGraph:
    """回放预设事件序列；记录传给 ``astream_events`` 的 config。"""

    def __init__(self, events):
        self._events = events
        self.captured = {}

    async def astream_events(self, input_state, config=None, version=None):
        self.captured = {
            "input_state": input_state,
            "config": config,
            "version": version,
        }
        for event in self._events:
            yield event


class _SlowStreamGraph:
    """每次产出事件前 sleep，用于触发流式墙钟守卫。"""

    def __init__(self, per_event_delay, n_events):
        self.per_event_delay = per_event_delay
        self.n_events = n_events

    async def astream_events(self, input_state, config=None, version=None):
        for _ in range(self.n_events):
            await asyncio.sleep(self.per_event_delay)
            yield _ev(
                "on_chat_model_stream",
                name="m",
                data={"chunk": _Chunk("x")},
                metadata={"langgraph_node": "final_answer"},
            )


# ---------------------------------------------------------------------------
# 1. 超时：AGENT_TIMEOUT 生效
# ---------------------------------------------------------------------------


class TestAgentTimeout:
    def test_ask_times_out_into_agent_timeout(self, monkeypatch):
        _patch_obs(monkeypatch)
        monkeypatch.setattr(settings_instance, "AGENT_TIMEOUT", 0.05)
        graph = _CapturingGraph(delay=0.3)
        service = ChatService(graph_getter=lambda: graph)

        with pytest.raises(HTTPException) as ei:
            _run(service.ask("hi", "s1"))

        exc = ei.value
        # AgentError(AGENT_TIMEOUT) 属 HTTP_FATAL → ask 的 P0-3 分类链映射为 504
        assert exc.status_code == 504
        assert exc.detail["error_code"] == ErrorCode.AGENT_TIMEOUT.value
        # 实现层确实抛的是 AgentError(AGENT_TIMEOUT)（挂在异常上下文上）
        cause = exc.__context__
        assert isinstance(cause, AgentError)
        assert cause.code is ErrorCode.AGENT_TIMEOUT

    def test_no_timeout_when_fast(self, monkeypatch):
        """未超时（默认 300s）时正常返回，证明不是无条件抛错。"""
        _patch_obs(monkeypatch)
        graph = _CapturingGraph(delay=0.0)
        service = ChatService(graph_getter=lambda: graph)
        out = _run(service.ask("hi", "s1"))
        assert out["status"] == "ok"
        assert out["message"] == "答复内容"


# ---------------------------------------------------------------------------
# 2. recursion_limit 接线
# ---------------------------------------------------------------------------


class TestRecursionLimitWiring:
    def test_ask_config_has_recursion_limit_at_top_level(self, monkeypatch):
        _patch_obs(monkeypatch)
        graph = _CapturingGraph()
        service = ChatService(graph_getter=lambda: graph)
        _run(service.ask("hi", "s1"))

        config = graph.captured_config
        assert config["recursion_limit"] == settings_instance.MAX_ITERATIONS
        # 必须是顶层键（放进 configurable 图不识别）
        assert "recursion_limit" not in config["configurable"]

    def test_stream_events_config_has_recursion_limit(self, monkeypatch):
        graph = _StreamGraph(
            events=[_ev("on_chain_end", name="LangGraph", metadata={})]
        )
        service = ChatService(graph_getter=lambda: graph)
        _collect(service.stream_events("hi", "s1"))

        config = graph.captured["config"]
        assert config["recursion_limit"] == settings_instance.MAX_ITERATIONS
        assert "recursion_limit" not in config["configurable"]


# ---------------------------------------------------------------------------
# 3. 错误分类
# ---------------------------------------------------------------------------


class TestAgentTimeoutClassification:
    def test_agent_timeout_is_degradable(self):
        assert ErrorCode.AGENT_TIMEOUT in DEGRADABLE

    def test_agent_timeout_http_fatal_is_504(self):
        assert HTTP_FATAL[ErrorCode.AGENT_TIMEOUT] == 504

    def test_error_code_value(self):
        assert ErrorCode.AGENT_TIMEOUT.value == "agent_timeout"


# ---------------------------------------------------------------------------
# 4-5. 检索重试：成功 / 耗尽
# ---------------------------------------------------------------------------


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
    from unittest.mock import MagicMock

    ks = MagicMock()
    ks.reranker.enabled = False
    ks.search = AsyncMock(return_value=[])
    return ks


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
        # ``X-API-Key``）。单列一个列表以免破坏既有的 ``posted`` 解包。
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
    """模拟 kb_ingest 未启动：post 直接抛传输层异常。"""

    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, url, json=None, headers=None):
        raise self._exc


class TestRetrievalRetry:
    def test_retry_then_success(self, monkeypatch):
        """首次传输失败（可重试）→ 二次 200 → 返回结果（证明 aretry 生效）。"""
        _patch_obs(monkeypatch)
        _no_sleep(monkeypatch)
        cfg = _make_cfg(postgres_search_url="http://kb/api/v1/knowledge/search")
        ks = _make_ks()

        sessions = [
            _FailingSession(aiohttp.ClientError("connection refused")),
            _FakeSession(
                _FakeResponse(200, {"results": [{"content": "c1", "similarity": 0.9}]})
            ),
        ]
        calls = {"n": 0}

        def factory(*args, **kwargs):
            idx = min(calls["n"], len(sessions) - 1)
            calls["n"] += 1
            return sessions[idx]

        monkeypatch.setattr(kb_retrieval.aiohttp, "ClientSession", factory)

        out = _run(kb_retrieval._postgres_search("民法典", cfg=cfg, knowledge_service=ks))

        assert calls["n"] == 2  # 恰好重试一次
        assert out and out[0]["tool"] == "postgres"

    def test_retry_exhausted_counts_channel_failure(self, monkeypatch):
        """检索通道持续失败 → 重试耗尽后向上抛 → hybrid_retrieve 记 failures。"""
        _patch_obs(monkeypatch)
        _no_sleep(monkeypatch)
        cfg = _make_cfg(postgres_search_url="http://kb/api/v1/knowledge/search")
        ks = _make_ks()
        monkeypatch.setattr(
            kb_retrieval.aiohttp,
            "ClientSession",
            lambda *a, **k: _FailingSession(aiohttp.ClientError("connection refused")),
        )

        async def fake_milvus(question, *, cfg, knowledge_service):
            return []

        monkeypatch.setattr(kb_retrieval, "_milvus_search", fake_milvus)

        out = _run(
            hybrid_retrieve("q", cfg=cfg, knowledge_service=ks, tools=["postgres", "milvus"])
        )
        assert out["failures"] == ["postgres"]
        assert out["degraded"] is True
        assert out["postgres"] == []

    def test_both_channels_exhausted_raises_retrieval_failed(self, monkeypatch):
        """两路都耗尽 → 仍抛 AgentError(RETRIEVAL_FAILED)（P1-5 语义不变）。"""
        _patch_obs(monkeypatch)
        _no_sleep(monkeypatch)
        cfg = _make_cfg(postgres_search_url="http://kb/api/v1/knowledge/search")
        ks = _make_ks()
        monkeypatch.setattr(
            kb_retrieval.aiohttp,
            "ClientSession",
            lambda *a, **k: _FailingSession(aiohttp.ClientError("connection refused")),
        )

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

    def test_aretry_relocation_shares_identity(self):
        """aretry 已从 infrastructure 上提到 domain.policies（re-export 同一对象）。"""
        from agentix.domain.policies import aretry as domain_aretry
        from agentix.infrastructure.llm.retry import aretry as infra_aretry

        assert domain_aretry is infra_aretry


# ---------------------------------------------------------------------------
# 6. 角色接线（静态）
# ---------------------------------------------------------------------------


class TestRoleWiring:
    def _src(self, rel):
        return (PROJECT_ROOT / rel).read_text(encoding="utf-8")

    def test_router_has_role(self):
        assert 'tags=["router"], role="router"' in self._src(
            "agentix/application/agents/router.py"
        )

    def test_hallucination_has_role(self):
        assert 'role="hallucination"' in self._src(
            "agentix/orchestration/langgraph/lg_builder.py"
        )

    def test_summarizer_has_role(self):
        assert 'role="summarizer"' in self._src(
            "agentix/application/services/task_handlers.py"
        )

    def test_general_has_explicit_role(self):
        assert 'role="general"' in self._src(
            "agentix/application/agents/general_handler.py"
        )

    def test_ambiguous_sites_remain_unwired(self):
        """同实例多角色的 4 处不接线（guardrails / image_handler 文件内无 role=）。"""
        assert "role=" not in self._src("agentix/application/agents/guardrails.py")
        assert "role=" not in self._src("agentix/application/agents/image_handler.py")


# ---------------------------------------------------------------------------
# 7. 埋点不影响主链路，且失败显式透出（R-P0-5）
# ---------------------------------------------------------------------------


class TestRetrievalInstrumentation:
    """R-P0-5：打点失败绝不打挂检索主链路，但也不许再静默吞掉。"""

    def _boom_obs(self, monkeypatch):
        import agentix.application.services.observability as obs_module

        def _boom():
            raise RuntimeError("redis down")

        monkeypatch.setattr(obs_module, "get_observability", _boom)

    @staticmethod
    def _capture_degraded(monkeypatch):
        """把 ``record_degraded`` 换成捕获器（R-P0-5 的显式透出面）。"""
        import agentix.observability.metrics as metrics_module

        captured: list = []
        monkeypatch.setattr(
            metrics_module, "record_degraded", lambda component: captured.append(component)
        )
        return captured

    def test_postgres_returns_and_marks_telemetry_degraded_when_obs_raises(
        self, monkeypatch
    ):
        self._boom_obs(monkeypatch)
        captured = self._capture_degraded(monkeypatch)
        cfg = _make_cfg(postgres_search_url="http://kb/api/v1/knowledge/search")
        ks = _make_ks()
        session = _FakeSession(
            _FakeResponse(200, {"results": [{"content": "c", "similarity": 0.9}]})
        )
        monkeypatch.setattr(kb_retrieval.aiohttp, "ClientSession", lambda *a, **k: session)

        out = _run(kb_retrieval._postgres_search("q", cfg=cfg, knowledge_service=ks))
        # 主链路照常返回（埋点失败 ≠ 检索失败）
        assert out and out[0]["tool"] == "postgres"
        # R-P0-5：失败显式透出为 degraded 计数（而非 debug 吞掉）
        assert "rag_retrieval_postgres" in captured

    def test_postgres_empty_result_returns_and_marks_telemetry_degraded(
        self, monkeypatch
    ):
        """HTTP 200 结构不符 → 返回 []（空结果非失败），埋点失败仍显式透出。"""
        self._boom_obs(monkeypatch)
        captured = self._capture_degraded(monkeypatch)
        cfg = _make_cfg(postgres_search_url="http://kb/api/v1/knowledge/search")
        ks = _make_ks()
        session = _FakeSession(_FakeResponse(200, {"results": "not-a-list"}))
        monkeypatch.setattr(kb_retrieval.aiohttp, "ClientSession", lambda *a, **k: session)

        out = _run(kb_retrieval._postgres_search("q", cfg=cfg, knowledge_service=ks))
        assert out == []
        assert "rag_retrieval_postgres" in captured

    def test_milvus_returns_and_marks_telemetry_degraded_when_obs_raises(
        self, monkeypatch
    ):
        self._boom_obs(monkeypatch)
        captured = self._capture_degraded(monkeypatch)
        cfg = _make_cfg()
        ks = _make_ks()
        ks.search = AsyncMock(return_value=[{"content": "m", "metadata": {}}])

        out = _run(kb_retrieval._milvus_search("q", cfg=cfg, knowledge_service=ks))
        assert out and out[0]["tool"] == "milvus"
        assert "rag_retrieval_milvus" in captured

    @pytest.mark.asyncio
    async def test_record_rag_retrieval_bool_contract(self, monkeypatch):
        """``_record_rag_retrieval`` 返回值契约：成功 True / 埋点降级 False。"""
        import agentix.application.services.observability as obs_module

        self._boom_obs(monkeypatch)
        assert await kb_retrieval._record_rag_retrieval("postgres", 1, 0.0) is False

        svc = AsyncMock()  # get_observability 成功路径
        monkeypatch.setattr(obs_module, "get_observability", lambda: svc)
        assert await kb_retrieval._record_rag_retrieval("postgres", 1, 0.0) is True
        svc.record_rag_retrieval.assert_awaited_once()

    def test_observability_records_on_success(self, monkeypatch):
        """正常路径确实调用了 record_rag_retrieval(source=..., result_count=...)。"""
        svc = _patch_obs(monkeypatch)
        cfg = _make_cfg(postgres_search_url="http://kb/api/v1/knowledge/search")
        ks = _make_ks()
        session = _FakeSession(
            _FakeResponse(200, {"results": [{"content": "c", "similarity": 0.9}]})
        )
        monkeypatch.setattr(kb_retrieval.aiohttp, "ClientSession", lambda *a, **k: session)

        _run(kb_retrieval._postgres_search("q", cfg=cfg, knowledge_service=ks))

        svc.record_rag_retrieval.assert_awaited()
        kwargs = svc.record_rag_retrieval.await_args.kwargs
        assert kwargs["source"] == "postgres"
        assert kwargs["result_count"] == 1


# ---------------------------------------------------------------------------
# 8. 流式墙钟守卫
# ---------------------------------------------------------------------------


class TestStreamWallClockGuard:
    def test_slow_stream_yields_error_and_single_done(self, monkeypatch):
        monkeypatch.setattr(settings_instance, "AGENT_TIMEOUT", 0.02)
        graph = _SlowStreamGraph(per_event_delay=0.1, n_events=10)
        service = ChatService(graph_getter=lambda: graph)

        events = _collect(service.stream_events("hi", "s1"))
        kinds = [e["type"] for e in events]

        assert "error" in kinds  # 超时经既有 error 事件机制收口
        assert kinds.count("done") == 1  # 恰好一次 done（兜底契约不破）
        assert kinds[0] == "metadata"  # 首帧 processing 保持

    def test_fast_stream_not_interrupted(self, monkeypatch):
        """正常（未超时）流不产生 error，done 恰好一次。"""
        monkeypatch.setattr(settings_instance, "AGENT_TIMEOUT", 5.0)
        graph = _StreamGraph(
            events=[
                _ev(
                    "on_chat_model_stream",
                    name="m",
                    data={"chunk": _Chunk("你好")},
                    metadata={"langgraph_node": "final_answer"},
                ),
                _ev("on_chain_end", name="LangGraph", metadata={}, data={"output": {}}),
            ]
        )
        service = ChatService(graph_getter=lambda: graph)

        events = _collect(service.stream_events("hi", "s1"))
        kinds = [e["type"] for e in events]

        assert "error" not in kinds
        assert kinds.count("done") == 1
        assert "message" in kinds
