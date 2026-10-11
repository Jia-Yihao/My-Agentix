"""P0-3 错误分类 + P0-4 可观测性接线测试。

P0-3：
- AgentError 载体与 ErrorCode/DEGRADABLE/HTTP_FATAL 分类表
- process_agent_query 的错误分类（503/500/degraded/legacy 回滚）
P0-4：
- TraceIdMiddleware（生成/透传/回写响应头）
- _TrackedChatOpenAI 的 LLM 指标上报（成功/失败路径）
"""

import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock

from agentix.config import settings as settings_instance
from agentix.domain.errors import (
    DEGRADABLE,
    HTTP_FATAL,
    AgentError,
    ErrorCode,
)
from agentix.infrastructure.llm.retry import is_retryable


# ---------------------------------------------------------------------------
# P0-3(a)：错误分类载体
# ---------------------------------------------------------------------------


class TestErrorTaxonomy:
    def test_error_code_values(self):
        assert ErrorCode.LLM_UNAVAILABLE.value == "llm_unavailable"
        assert ErrorCode.NO_EVIDENCE.value == "no_evidence"

    def test_agent_error_attributes(self):
        err = AgentError(
            ErrorCode.LLM_TIMEOUT, "timeout", detail="stack", retryable=True
        )
        assert err.code is ErrorCode.LLM_TIMEOUT
        assert err.message == "timeout"
        assert err.detail == "stack"
        assert err.retryable is True

    def test_http_fatal_mapping(self):
        assert HTTP_FATAL[ErrorCode.LLM_UNAVAILABLE] == 503
        assert HTTP_FATAL[ErrorCode.LLM_RATE_LIMIT] == 429
        assert HTTP_FATAL[ErrorCode.LLM_TIMEOUT] == 504

    def test_degradable_membership(self):
        assert ErrorCode.LLM_TIMEOUT in DEGRADABLE
        assert ErrorCode.GRAPH_UNAVAILABLE in DEGRADABLE
        # 业务语义（无结果/越界）不是错误，不在可降级集合
        assert ErrorCode.NO_EVIDENCE not in DEGRADABLE
        assert ErrorCode.OUT_OF_SCOPE not in DEGRADABLE


# ---------------------------------------------------------------------------
# P0-3(b)：process_agent_query 错误分类
# ---------------------------------------------------------------------------


class _FakeGraph:
    """mock lg_builder.graph——ainvoke 可配置为返回结果或抛异常。"""

    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc
        self.calls = 0

    async def ainvoke(self, input_state, config=None):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._result


def _ok_result(degraded=False):
    from langchain_core.messages import AIMessage

    msg = AIMessage(content="答复内容")
    if degraded:
        msg.additional_kwargs["degraded"] = True
    return {
        "messages": [msg],
        "router": {"type": "legal", "logic": "test"},
        "sources": [],
    }


@pytest.fixture
def _obs_mock(monkeypatch):
    """mock observability 服务，避免测试依赖 Redis。"""
    import agentix.application.services.observability as obs_module

    svc = AsyncMock()
    svc.record_request = AsyncMock()
    monkeypatch.setattr(obs_module, "get_observability", lambda: svc)
    return svc


@pytest.fixture
def _patched_graph():
    """patch lg_builder.graph，返回可控 fake；结束后还原。"""
    from agentix.orchestration.langgraph import lg_builder

    original = lg_builder.graph
    yield lg_builder
    lg_builder.graph = original


class TestProcessAgentQueryErrorClassification:
    @pytest.mark.asyncio
    async def test_success_returns_ok(self, monkeypatch, _patched_graph, _obs_mock):
        from agentix.interfaces.http.v1.chat import process_agent_query

        settings_instance.LEGACY_ERROR_MODE = False
        _patched_graph.graph = _FakeGraph(result=_ok_result())

        result = await process_agent_query("你好", "s1")
        assert result["status"] == "ok"
        assert result["message"] == "答复内容"
        assert result["error_code"] is None

    @pytest.mark.asyncio
    async def test_degraded_flag_from_fallback(
        self, monkeypatch, _patched_graph, _obs_mock
    ):
        from agentix.interfaces.http.v1.chat import process_agent_query

        settings_instance.LEGACY_ERROR_MODE = False
        _patched_graph.graph = _FakeGraph(result=_ok_result(degraded=True))

        result = await process_agent_query("你好", "s1")
        assert result["status"] == "degraded"

    @pytest.mark.asyncio
    async def test_transient_error_maps_to_503(
        self, monkeypatch, _patched_graph, _obs_mock
    ):
        from agentix.interfaces.http.v1.chat import process_agent_query

        settings_instance.LEGACY_ERROR_MODE = False
        # 连接类异常 → is_retryable=True → 503 llm_unavailable
        _patched_graph.graph = _FakeGraph(exc=ConnectionError("connection refused"))

        with pytest.raises(HTTPException) as exc_info:
            await process_agent_query("你好", "s1")
        assert exc_info.value.status_code == 503
        assert exc_info.value.detail["error_code"] == "llm_unavailable"

    @pytest.mark.asyncio
    async def test_unexpected_error_maps_to_500(
        self, monkeypatch, _patched_graph, _obs_mock
    ):
        from agentix.interfaces.http.v1.chat import process_agent_query

        settings_instance.LEGACY_ERROR_MODE = False
        _patched_graph.graph = _FakeGraph(exc=ValueError("bad input shape"))

        with pytest.raises(HTTPException) as exc_info:
            await process_agent_query("你好", "s1")
        assert exc_info.value.status_code == 500
        assert exc_info.value.detail["error_code"] == "internal_error"

    @pytest.mark.asyncio
    async def test_legacy_mode_restores_swallow(self, monkeypatch, _patched_graph, _obs_mock):
        from agentix.interfaces.http.v1.chat import process_agent_query

        settings_instance.LEGACY_ERROR_MODE = True  # 回滚开关：旧行为
        _patched_graph.graph = _FakeGraph(exc=ValueError("bad input shape"))

        result = await process_agent_query("你好", "s1")
        assert result["status"] == "degraded"
        assert result["error_code"] == "internal_error"
        assert "错误" in result["message"]

    @pytest.mark.asyncio
    async def test_agent_error_fatal_raises(self, monkeypatch, _patched_graph, _obs_mock):
        from agentix.interfaces.http.v1.chat import process_agent_query

        settings_instance.LEGACY_ERROR_MODE = False
        _patched_graph.graph = _FakeGraph(
            exc=AgentError(ErrorCode.LLM_UNAVAILABLE, "model down")
        )

        with pytest.raises(HTTPException) as exc_info:
            await process_agent_query("你好", "s1")
        assert exc_info.value.status_code == 503
        assert exc_info.value.detail["error_code"] == "llm_unavailable"

    @pytest.mark.asyncio
    async def test_agent_error_degradable_returns_200(
        self, monkeypatch, _patched_graph, _obs_mock
    ):
        from agentix.interfaces.http.v1.chat import process_agent_query

        settings_instance.LEGACY_ERROR_MODE = False
        _patched_graph.graph = _FakeGraph(
            exc=AgentError(ErrorCode.RETRIEVAL_FAILED, "retrieval down")
        )

        result = await process_agent_query("你好", "s1")
        assert result["status"] == "degraded"
        assert result["error_code"] == "retrieval_failed"
        assert "暂时不可用" in result["message"]


# ---------------------------------------------------------------------------
# P0-4：TraceIdMiddleware + _TrackedChatOpenAI
# ---------------------------------------------------------------------------


class TestTraceIdMiddleware:
    def _build_app(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from agentix.interfaces.http.middleware import TraceIdMiddleware

        app = FastAPI()
        app.add_middleware(TraceIdMiddleware)

        @app.get("/ping")
        async def ping():
            from agentix.interfaces.http.middleware import trace_id_var

            return {"trace_id": trace_id_var.get()}

        return TestClient(app)

    def test_generates_uuid_when_absent(self):
        client = self._build_app()
        resp = client.get("/ping")
        assert resp.status_code == 200
        tid = resp.headers["X-Trace-Id"]
        assert len(tid) == 32  # uuid4().hex
        assert resp.json()["trace_id"] == tid

    def test_honors_incoming_trace_id(self):
        client = self._build_app()
        resp = client.get("/ping", headers={"X-Trace-Id": "my-trace-123"})
        assert resp.headers["X-Trace-Id"] == "my-trace-123"
        assert resp.json()["trace_id"] == "my-trace-123"


class TestTrackedChatOpenAI:
    def _make_client(self):
        from agentix.application.agents.llm_factory import _TrackedChatOpenAI

        return _TrackedChatOpenAI(
            openai_api_key="test-key",
            model_name="test-model",
            openai_api_base="http://localhost:1234/v1",
            role_tag="router",
        )

    @pytest.fixture
    def _obs_mock2(self, monkeypatch):
        import agentix.application.services.observability as obs_module

        svc = AsyncMock()
        svc.record_llm_call = AsyncMock()
        monkeypatch.setattr(obs_module, "get_observability", lambda: svc)
        return svc

    @pytest.mark.asyncio
    async def test_success_records_llm_call(self, monkeypatch, _obs_mock2):
        from langchain_core.messages import AIMessage

        from agentix.interfaces.http.middleware import trace_id_var

        trace_id_var.set("trace-unit-1")

        async def _fake_ainvoke(self, *args, **kwargs):
            msg = AIMessage(content="hi")
            msg.usage_metadata = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
            return msg

        from langchain_openai import ChatOpenAI

        monkeypatch.setattr(ChatOpenAI, "ainvoke", _fake_ainvoke)

        client = self._make_client()
        res = await client.ainvoke("hello")
        assert res.content == "hi"

        _obs_mock2.record_llm_call.assert_awaited_once()
        metrics = _obs_mock2.record_llm_call.await_args.args[0]
        assert metrics.success is True
        assert metrics.total_tokens == 15
        assert "router" in metrics.tags
        assert "trace-unit-1" in metrics.tags

    @pytest.mark.asyncio
    async def test_failure_records_and_reraises(self, monkeypatch, _obs_mock2):
        from langchain_openai import ChatOpenAI

        from agentix.interfaces.http.middleware import trace_id_var

        trace_id_var.set("trace-unit-2")

        async def _fake_ainvoke(self, *args, **kwargs):
            raise ConnectionError("upstream down")

        monkeypatch.setattr(ChatOpenAI, "ainvoke", _fake_ainvoke)

        client = self._make_client()
        with pytest.raises(ConnectionError):
            await client.ainvoke("hello")

        _obs_mock2.record_llm_call.assert_awaited_once()
        metrics = _obs_mock2.record_llm_call.await_args.args[0]
        assert metrics.success is False
        assert "ConnectionError" in metrics.error

    @pytest.mark.asyncio
    async def test_metrics_failure_never_breaks_main_flow(
        self, monkeypatch, _obs_mock2
    ):
        """指标上报抛异常时主链路不受影响（方案 P0-4 风险缓解）。"""
        from langchain_core.messages import AIMessage
        from langchain_openai import ChatOpenAI

        _obs_mock2.record_llm_call = AsyncMock(side_effect=RuntimeError("redis down"))

        async def _fake_ainvoke(self, *args, **kwargs):
            return AIMessage(content="hi")

        monkeypatch.setattr(ChatOpenAI, "ainvoke", _fake_ainvoke)

        client = self._make_client()
        res = await client.ainvoke("hello")  # 不应抛 Redis 异常
        assert res.content == "hi"


class TestGetLlmClientRoleTag:
    def test_client_carries_role_tag(self, monkeypatch):
        from agentix.application.agents.llm_factory import get_llm_client

        if not settings_instance.OPENAI_API_KEY:
            monkeypatch.setattr(
                settings_instance, "OPENAI_API_KEY", "test-key", raising=False
            )
        client = get_llm_client(role="router")
        assert client.role_tag == "router"
        assert client.temperature == 0.0

    def test_is_retryable_still_works(self):
        # P0-3 启发式分类依赖 retry.py 的分类器
        assert is_retryable(ConnectionError("refused")) is True
        assert is_retryable(ValueError("bad")) is False
