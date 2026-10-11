"""Unit tests for observability module."""

from agentix.application.services.observability import (
    LLMCallMetrics,
    RequestMetrics,
    MetricsTracker,
)


class TestLLMCallMetrics:
    """Tests for LLMCallMetrics dataclass."""

    def test_default_values(self):
        m = LLMCallMetrics(model="gpt-4")
        assert m.model == "gpt-4"
        assert m.prompt_tokens == 0
        assert m.completion_tokens == 0
        assert m.total_tokens == 0
        assert m.latency_ms == 0.0
        assert m.success is True
        assert m.error is None
        assert m.tags == []

    def test_custom_values(self):
        m = LLMCallMetrics(
            model="qwen3-max",
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            latency_ms=500.0,
            success=False,
            error="timeout",
            tags=["router"],
        )
        assert m.total_tokens == 150
        assert m.success is False
        assert m.error == "timeout"
        assert m.tags == ["router"]


class TestRequestMetrics:
    """Tests for RequestMetrics dataclass."""

    def test_default_values(self):
        m = RequestMetrics(request_id="req1", session_id="sess1")
        assert m.request_id == "req1"
        assert m.session_id == "sess1"
        assert m.route_type == ""
        assert m.total_latency_ms == 0.0
        assert m.llm_calls == 0
        assert m.total_tokens == 0
        assert m.tools_used == []
        assert m.success is True

    def test_with_tools(self):
        m = RequestMetrics(
            request_id="req2",
            session_id="sess2",
            route_type="graphrag-query",
            tools_used=["cypher_query", "text2sql"],
            total_tokens=500,
        )
        assert len(m.tools_used) == 2
        assert m.route_type == "graphrag-query"


class TestMetricsTracker:
    """Tests for MetricsTracker context manager."""

    def test_add_llm_call(self):
        tracker = MetricsTracker.__new__(MetricsTracker)
        tracker.llm_calls = 0
        tracker.total_tokens = 0

        tracker.add_llm_call(tokens=100)
        assert tracker.llm_calls == 1
        assert tracker.total_tokens == 100

        tracker.add_llm_call(tokens=200)
        assert tracker.llm_calls == 2
        assert tracker.total_tokens == 300

    def test_add_tool(self):
        tracker = MetricsTracker.__new__(MetricsTracker)
        tracker.tools_used = []

        tracker.add_tool("cypher_query")
        tracker.add_tool("text2sql")
        tracker.add_tool("cypher_query")  # duplicate

        assert len(tracker.tools_used) == 2
        assert "cypher_query" in tracker.tools_used
        assert "text2sql" in tracker.tools_used

    def test_set_route(self):
        tracker = MetricsTracker.__new__(MetricsTracker)
        tracker.route_type = ""

        tracker.set_route("kb-query")
        assert tracker.route_type == "kb-query"
