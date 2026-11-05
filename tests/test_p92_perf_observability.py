"""性能真值可观测性守卫（p92）。

覆盖：

1. 指标助手：TTFT 直方图 / 角色 LLM 调用与 token 账本 / 事件循环滞后；
2. HTTP 请求指标接线：RequestLoggingMiddleware → ``record_request``，
   endpoint 归一化（数字/UUID/长十六进制 → {id}，段数封顶）；
3. RunEventEmitter 性能戳记：首内容帧 TTFT、终帧 duration_ms/ttft_ms、
   ``LATENCY_BREAKDOWN_ENABLED`` 门控 breakdown、``LATENCY_TOTAL_BUDGET_MS``
   预算评估 fail-open、任何指标故障都绝不打挂帧流；
4. agentix.performance 包：latency（capture/phase）与 budget（evaluate/enforce）；
5. 事件循环滞后采样器单 tick；
6. 角色账本两条生产接线：编排域 ``_TrackedChatOpenAI.ainvoke``、
   检索域 ``chat_llm.record_llm_usage``。

全部断言在 flag 默认态下不改变行为（flag-off 逐位等价由 p65/p41 复核）。
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from agentix.application.services.run_event_stream import RunEventEmitter
from agentix.main import _normalize_endpoint


# ---------------------------------------------------------------------------
# 1. 指标助手
# ---------------------------------------------------------------------------


def _scrape() -> str:
    """抓取进程内 Prometheus 文本（prometheus_client 缺失时返回空串）。"""
    from agentix.observability import metrics as m

    if not m.PROMETHEUS_AVAILABLE:
        return ""
    from prometheus_client import generate_latest

    return generate_latest().decode("utf-8", errors="replace")


class TestMetricHelpers:
    def test_ttft_histogram_records(self):
        from prometheus_client import REGISTRY

        from agentix.observability.metrics import record_ttft

        # histogram 的 bucket/count 曝光行恒存在（值 0 也在），必须读值不数行
        before = REGISTRY.get_sample_value("agent_ttft_seconds_count") or 0.0
        record_ttft(0.25)
        after = REGISTRY.get_sample_value("agent_ttft_seconds_count") or 0.0
        assert after == before + 1.0
        assert (REGISTRY.get_sample_value("agent_ttft_seconds_sum") or 0.0) >= 0.25

    def test_agent_llm_call_and_tokens_counters(self):
        from agentix.observability.metrics import (
            record_agent_llm_call,
            record_agent_llm_tokens,
        )

        record_agent_llm_call(role="planner", status="success")
        record_agent_llm_call(role="retrieval", status="error")
        record_agent_llm_tokens(role="retrieval", prompt_tokens=11, completion_tokens=7)
        body = _scrape()
        assert 'agent_llm_call_total{role="planner",status="success"}' in body
        assert 'agent_llm_call_total{role="retrieval",status="error"}' in body
        assert 'agent_llm_tokens_total{role="retrieval",type="prompt"}' in body
        assert 'agent_llm_tokens_total{role="retrieval",type="completion"}' in body

    def test_event_loop_lag_helper_records(self):
        from prometheus_client import REGISTRY

        from agentix.observability.metrics import record_event_loop_lag

        before = REGISTRY.get_sample_value("event_loop_lag_seconds_count") or 0.0
        record_event_loop_lag(0.012)
        after = REGISTRY.get_sample_value("event_loop_lag_seconds_count") or 0.0
        assert after == before + 1.0

    def test_zero_token_calls_not_recorded(self):
        from agentix.observability.metrics import record_agent_llm_tokens

        # 0 token 不推进任何序列（与 record_llm_tokens 同纪律）
        record_agent_llm_tokens(role="general", prompt_tokens=0, completion_tokens=0)
        # 不抛异常即通过；标签存在性由上面用例覆盖


# ---------------------------------------------------------------------------
# 2. endpoint 归一化 + 中间件接线
# ---------------------------------------------------------------------------


class TestEndpointNormalization:
    def test_numeric_id_collapsed(self):
        assert _normalize_endpoint("/v1/users/12345") == "/v1/users/{id}"

    def test_uuid_collapsed(self):
        uuid = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
        assert _normalize_endpoint(f"/api/v1/agent/runs/{uuid}/chat") == (
            "/api/v1/agent/runs/{id}/chat"
        )

    def test_long_hex_collapsed(self):
        assert _normalize_endpoint("/v1/kb/" + "a1" * 16 + "/docs") == (
            "/v1/kb/{id}/docs"
        )

    def test_static_segments_kept(self):
        assert _normalize_endpoint("/api/v1/health") == "/api/v1/health"

    def test_root_and_empty(self):
        assert _normalize_endpoint("/") == "/"
        assert _normalize_endpoint("") == "/"

    def test_query_string_stripped(self):
        assert _normalize_endpoint("/v1/users/123?x=1") == "/v1/users/{id}"

    def test_segment_cap_bounds_cardinality(self):
        path = "/" + "/".join(f"seg{i}" for i in range(12))
        endpoint = _normalize_endpoint(path)
        assert endpoint.endswith("/*")
        assert endpoint.count("/") <= 7  # 6 段 + 封顶的 * 分量


class TestRequestMetricsWiring:
    def test_middleware_records_request(self, monkeypatch):
        """真实 ASGI 请求 → 中间件调用 record_request（method/endpoint/status/时延）。"""
        captured: list = []

        import agentix.observability.metrics as m

        monkeypatch.setattr(
            m,
            "record_request",
            lambda method, endpoint, status, duration: captured.append(
                (method, endpoint, status, duration)
            ),
        )

        from fastapi.testclient import TestClient

        from agentix.main import application

        with TestClient(application) as client:
            resp = client.get("/liveness")
            assert resp.status_code == 200

        assert len(captured) == 1
        method, endpoint, status, duration = captured[0]
        assert method == "GET"
        assert endpoint == "/liveness"
        assert status == 200
        assert duration >= 0


# ---------------------------------------------------------------------------
# 3. RunEventEmitter 性能戳记
# ---------------------------------------------------------------------------


def _fresh_emitter() -> RunEventEmitter:
    return RunEventEmitter(run_id="run-p92")


class TestEmitterPerfStamping:
    def test_ttft_recorded_once_on_first_content(self, monkeypatch):
        seen: list = []

        import agentix.observability.metrics as m

        monkeypatch.setattr(m, "record_ttft", lambda d: seen.append(d))

        em = _fresh_emitter()
        em.attach({"type": "answer_chunk", "content": "a"})
        em.attach({"type": "answer_chunk", "content": "b"})
        em.attach({"type": "message", "content": "c"})  # 编排域词汇
        assert len(seen) == 1
        assert em.ttft_ms is not None and em.ttft_ms >= 0

    def test_terminal_frames_get_duration_and_ttft(self):
        em = _fresh_emitter()
        em.attach({"type": "answer_chunk", "content": "x"})
        frame = em.attach({"type": "finished", "answer": "done"})
        assert isinstance(frame["duration_ms"], int) and frame["duration_ms"] >= 0
        assert isinstance(frame["ttft_ms"], int) and frame["ttft_ms"] >= 0
        assert frame["seq"] == 2 and frame["run_id"] == "run-p92"

    @pytest.mark.parametrize("terminal", ["finished", "done", "error"])
    def test_all_terminal_vocab_stamped(self, terminal):
        em = _fresh_emitter()
        frame = em.attach({"type": terminal})
        assert "duration_ms" in frame

    def test_no_content_frame_means_no_ttft_on_terminal(self):
        em = _fresh_emitter()
        frame = em.attach({"type": "error", "code": "X"})
        assert "duration_ms" in frame
        assert "ttft_ms" not in frame

    def test_breakdown_flag_off_by_default(self, monkeypatch):
        from agentix.config import settings

        monkeypatch.setattr(settings, "LATENCY_BREAKDOWN_ENABLED", False)
        em = _fresh_emitter()
        frame = em.attach({"type": "finished"})
        assert "breakdown_ms" not in frame

    def test_breakdown_flag_on_injects_ttft_retrieval_stream(
        self, monkeypatch
    ):
        from agentix.config import settings

        monkeypatch.setattr(settings, "LATENCY_BREAKDOWN_ENABLED", True)
        em = _fresh_emitter()
        em.attach(
            {
                "type": "progress",
                "detail": {"stages": [{"stage": "a", "ms": 10}, {"stage": "b", "ms": 5}]},
            }
        )
        em.attach({"type": "answer_chunk", "content": "x"})
        frame = em.attach({"type": "finished"})
        breakdown = frame["breakdown_ms"]
        assert breakdown["retrieval_ms"] == 15
        assert breakdown["stream_ms"] >= 0
        assert isinstance(breakdown["ttft_ms"], int)

    def test_budget_flag_wires_enforce(self, monkeypatch):
        """budget >0 时终帧走 enforce（判定本身由 budget 层单测覆盖）。"""
        from agentix.config import settings
        from agentix.performance import budget as budget_mod

        calls: list = []
        monkeypatch.setattr(settings, "LATENCY_TOTAL_BUDGET_MS", 100)
        monkeypatch.setattr(
            budget_mod,
            "enforce",
            lambda total_ms, source="stream": calls.append((total_ms, source)) or None,
        )
        em = _fresh_emitter()
        em.attach({"type": "finished"})
        assert len(calls) == 1
        assert calls[0][0] >= 0 and calls[0][1] == "run_stream"

    def test_budget_off_no_enforce(self, monkeypatch):
        from agentix.config import settings
        from agentix.performance import budget as budget_mod

        calls: list = []
        monkeypatch.setattr(settings, "LATENCY_TOTAL_BUDGET_MS", 0)
        monkeypatch.setattr(budget_mod, "enforce", lambda *a, **k: calls.append(1))
        em = _fresh_emitter()
        em.attach({"type": "finished"})
        assert calls == []

    def test_metric_failure_never_breaks_frame(self, monkeypatch):
        import agentix.observability.metrics as m

        def boom(_d):
            raise RuntimeError("metrics exploded")

        monkeypatch.setattr(m, "record_ttft", boom)
        em = _fresh_emitter()
        f1 = em.attach({"type": "answer_chunk", "content": "x"})
        f2 = em.attach({"type": "finished"})
        assert f1["seq"] == 1 and f2["seq"] == 2
        assert "duration_ms" in f2


# ---------------------------------------------------------------------------
# 4. agentix.performance：latency / budget
# ---------------------------------------------------------------------------


class TestLatencyBreakdown:
    def test_capture_and_phase_records(self):
        from agentix.performance import latency

        with latency.capture() as bd:
            assert latency.current() is bd
            with latency.phase("retrieval"):
                time.sleep(0.002)
            with latency.phase("stream"):
                pass
        data = bd.to_dict()
        assert set(data) == {"retrieval", "stream"}
        assert data["retrieval"] >= 1
        assert data["stream"] >= 0
        assert bd.total_ms >= data["retrieval"]
        assert [s.name for s in bd.samples] == ["retrieval", "stream"]

    def test_phase_without_capture_is_noop(self):
        from agentix.performance import latency

        latency.reset()
        assert latency.current() is None
        with latency.phase("orphan"):  # 不绑账本不炸不记
            pass
        assert latency.current() is None

    def test_phase_records_on_exception(self):
        from agentix.performance import latency

        with latency.capture() as bd:
            with pytest.raises(ValueError):
                with latency.phase("boom"):
                    raise ValueError("x")
        assert "boom" in bd.to_dict()


class TestBudget:
    def test_off_by_default_evaluates_none(self, monkeypatch):
        from agentix.config import settings
        from agentix.performance import budget

        monkeypatch.setattr(settings, "LATENCY_TOTAL_BUDGET_MS", 0)
        assert budget.evaluate(999999) is None
        assert budget.enforce(999999) is None

    def test_ok_and_over_verdicts(self, monkeypatch):
        from agentix.config import settings
        from agentix.performance import budget

        monkeypatch.setattr(settings, "LATENCY_TOTAL_BUDGET_MS", 100)
        assert budget.evaluate(50) == "ok"
        assert budget.evaluate(150) == "over"

    def test_enforce_over_logs_and_degrades(self, monkeypatch):
        from agentix.config import settings
        from agentix.observability import metrics as m
        from agentix.performance import budget

        degraded: list = []
        monkeypatch.setattr(settings, "LATENCY_TOTAL_BUDGET_MS", 100)
        monkeypatch.setattr(budget, "_OVER_WARNED", False)
        monkeypatch.setattr(m, "record_degraded", lambda c: degraded.append(c))
        assert budget.enforce(500, source="unit") == "over"
        assert degraded == ["latency_budget"]
        # 二次触发只补计数（不再重复 warning 也照样记账）
        assert budget.enforce(600, source="unit") == "over"
        assert degraded == ["latency_budget", "latency_budget"]

    def test_enforce_fail_open_when_degrade_itself_fails(self, monkeypatch):
        from agentix.config import settings
        from agentix.observability import metrics as m
        from agentix.performance import budget

        def boom(_c):
            raise RuntimeError("degrade exploded")

        monkeypatch.setattr(settings, "LATENCY_TOTAL_BUDGET_MS", 100)
        monkeypatch.setattr(budget, "_OVER_WARNED", False)
        monkeypatch.setattr(m, "record_degraded", boom)
        assert budget.enforce(500, source="unit") == "over"  # 留痕失败不改判词


# ---------------------------------------------------------------------------
# 5. 事件循环滞后采样器
# ---------------------------------------------------------------------------


class TestEventLoopLagSampler:
    @pytest.mark.asyncio
    async def test_sampler_emits_at_least_one_sample(self, monkeypatch):
        from agentix.observability import metrics as m
        import agentix.main as main_mod

        seen: list = []
        monkeypatch.setattr(m, "record_event_loop_lag", lambda lag: seen.append(lag))

        task = asyncio.get_running_loop().create_task(main_mod._event_loop_lag_sampler())
        try:
            await asyncio.sleep(0.65)  # 0.5s 间隔 → 至少一个 tick
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert len(seen) >= 1
        assert seen[0] >= 0

    def test_flag_defaults_off(self):
        from agentix.config import settings

        assert settings.EVENT_LOOP_LAG_METRIC_ENABLED is False


# ---------------------------------------------------------------------------
# 6. 角色账本生产接线
# ---------------------------------------------------------------------------


class TestRoleAccountingWiring:
    @pytest.mark.asyncio
    async def test_tracked_chat_openai_records_role_metrics(self, monkeypatch):
        from langchain_openai import ChatOpenAI

        from agentix.application.agents import llm_factory as lf
        from agentix.observability import metrics as m

        calls: list = []
        tokens: list = []
        monkeypatch.setattr(
            m, "record_agent_llm_call", lambda role, status: calls.append((role, status))
        )
        monkeypatch.setattr(
            m,
            "record_agent_llm_tokens",
            lambda role, prompt_tokens, completion_tokens: tokens.append(
                (role, prompt_tokens, completion_tokens)
            ),
        )

        class _Resp:
            usage_metadata = {"input_tokens": 7, "output_tokens": 5, "total_tokens": 12}
            content = "ok"

        async def fake_ainvoke(self, *a, **kw):
            return _Resp()

        monkeypatch.setattr(ChatOpenAI, "ainvoke", fake_ainvoke)

        client = lf._TrackedChatOpenAI(
            openai_api_key="sk-test", model_name="m", role_tag="router"
        )
        await client.ainvoke("hi")

        assert ("router", "success") in calls
        assert ("router", 7, 5) in tokens

    @pytest.mark.asyncio
    async def test_tracked_chat_openai_records_failure_status(self, monkeypatch):
        from langchain_openai import ChatOpenAI

        from agentix.application.agents import llm_factory as lf
        from agentix.observability import metrics as m

        calls: list = []
        monkeypatch.setattr(
            m, "record_agent_llm_call", lambda role, status: calls.append((role, status))
        )

        async def fake_ainvoke(self, *a, **kw):
            raise RuntimeError("llm down")

        monkeypatch.setattr(ChatOpenAI, "ainvoke", fake_ainvoke)

        client = lf._TrackedChatOpenAI(
            openai_api_key="sk-test", model_name="m", role_tag="planner"
        )
        with pytest.raises(RuntimeError):
            await client.ainvoke("hi")

        assert ("planner", "error") in calls

    @pytest.mark.asyncio
    async def test_retrieval_record_llm_usage_records_role_metrics(self, monkeypatch):
        from agentix.observability import metrics as m
        from agentix.retrieval.usecases import chat_llm

        calls: list = []
        tokens: list = []
        monkeypatch.setattr(
            m, "record_agent_llm_call", lambda role, status: calls.append((role, status))
        )
        monkeypatch.setattr(
            m,
            "record_agent_llm_tokens",
            lambda role, prompt_tokens, completion_tokens: tokens.append(
                (role, prompt_tokens, completion_tokens)
            ),
        )

        class _Session:
            def add(self, *_a, **_kw):
                return None

            def commit(self):
                return None

            def rollback(self):
                return None

        chat_llm.record_llm_usage(
            _Session(),
            request_id="req-p92",
            model_name="m",
            tokens_in=9,
            tokens_out=3,
        )
        assert ("retrieval", "success") in calls
        assert ("retrieval", 9, 3) in tokens
