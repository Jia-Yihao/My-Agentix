"""Phase H·W0 性能冒烟（CI perf-smoke job 入口，软断言）。

定位：**软门禁**——用进程内假件跑真实代码路径，只拦「量级级」退化
（单次流 > PERF_SMOKE_AVG_MS 或 HTTP > PERF_SMOKE_HTTP_AVG_MS，默认 3s/2s，
可经环境变量放宽）；精细回归判断交给 evals/performance/perf_gate.py
（continue-on-error 的 p50×3 基线比对）。

除耗时外，本文件还承担两条**正确性**硬断言（与耗时无关，永不放宽）：
流终帧必须带 W0 的 duration_ms/ttft_ms 戳记；中间件必须把请求记进
``http_requests_total``（endpoint 归一化接线的端到端证明）。
"""

from __future__ import annotations

import os
import time

import pytest

from evals.performance.fakes import collect_stream, patched_stream, reset_state

STREAM_AVG_BUDGET_MS = float(os.environ.get("PERF_SMOKE_AVG_MS", "3000"))
HTTP_AVG_BUDGET_MS = float(os.environ.get("PERF_SMOKE_HTTP_AVG_MS", "2000"))
STREAM_ITERS = int(os.environ.get("PERF_SMOKE_ITERS", "5"))
HTTP_ITERS = int(os.environ.get("PERF_SMOKE_HTTP_ITERS", "3"))


@pytest.fixture(autouse=True)
def _fresh_state():
    reset_state()
    yield
    reset_state()


class TestStreamPerfSmoke:
    @pytest.mark.asyncio
    async def test_stream_wall_time_and_stamps_soft(self, monkeypatch):
        """5 轮流全链：软耗时 + 硬戳记断言（duration/ttft）。"""
        from agentix.config import settings
        from agentix.observability import metrics as m

        ttfts: list = []
        monkeypatch.setattr(m, "record_ttft", lambda d: ttfts.append(d))
        monkeypatch.setattr(settings, "LATENCY_BREAKDOWN_ENABLED", True)

        durations: list = []
        frames = None
        with patched_stream(include_progress=True):
            for _ in range(STREAM_ITERS):
                t0 = time.perf_counter()
                frames = await collect_stream()
                durations.append((time.perf_counter() - t0) * 1000.0)

        avg = sum(durations) / len(durations)
        assert avg <= STREAM_AVG_BUDGET_MS, f"流均值 {avg:.0f}ms 超软预算 {STREAM_AVG_BUDGET_MS}ms"

        # 正确性硬断言（不放宽）
        assert frames, "流必须产出帧"
        terminal = frames[-1]
        assert terminal["type"] == "answer_chunk" and terminal.get("finished") is True
        assert isinstance(terminal.get("duration_ms"), int)
        assert isinstance(terminal.get("ttft_ms"), int)
        assert terminal["seq"] == len(frames)
        assert len(ttfts) == STREAM_ITERS, "每轮流恰好一次 TTFT 打点"
        # breakdown flag-on：progress 四阶段被汇总进 retrieval_ms
        breakdown = terminal.get("breakdown_ms")
        assert isinstance(breakdown, dict)
        assert breakdown["retrieval_ms"] == 29

    @pytest.mark.asyncio
    async def test_flag_off_terminal_has_no_breakdown(self, monkeypatch):
        from agentix.config import settings

        monkeypatch.setattr(settings, "LATENCY_BREAKDOWN_ENABLED", False)
        with patched_stream():
            frames = await collect_stream()
        terminal = frames[-1]
        assert "breakdown_ms" not in terminal  # flag-off 与基线逐位等价
        assert isinstance(terminal.get("duration_ms"), int)  # 但 duration 恒注入


class TestHttpPerfSmoke:
    def test_http_endpoints_soft_and_wiring_hard(self):
        """HTTP 族软耗时 + record_request 端到端接线硬断言。"""
        from fastapi.testclient import TestClient

        from agentix.main import application

        durations: list = []
        with TestClient(application) as client:
            for _ in range(HTTP_ITERS):
                for path in ("/liveness", "/metrics"):
                    t0 = time.perf_counter()
                    resp = client.get(path)
                    durations.append((time.perf_counter() - t0) * 1000.0)
                    assert resp.status_code == 200
            metrics_body = client.get("/metrics").text

        avg = sum(durations) / len(durations)
        assert avg <= HTTP_AVG_BUDGET_MS, f"HTTP 均值 {avg:.0f}ms 超软预算 {HTTP_AVG_BUDGET_MS}ms"

        # 硬断言：中间件把真实请求记进了 Prometheus（endpoint 已归一化）
        assert 'endpoint="/liveness"' in metrics_body
        assert 'endpoint="/metrics"' in metrics_body
