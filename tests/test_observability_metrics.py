"""企业化 P0-1：/metrics 端点暴露验证。

验收（评估报告 §3 P0-1）：
1. GET /metrics 返回 200，Content-Type 为 Prometheus 文本格式；
2. 响应体包含已注册的指标族（如 http_requests_total / python_info）；
3. prometheus_client 缺失时优雅降级（空正文 200，不 5xx）；
4. /metrics 在 APIKeyMiddleware 免鉴权白名单中（REQUIRE_API_KEY=true 时仍可抓取）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import agentix.observability.metrics as metrics_module
from agentix.observability.metrics import record_request


@pytest.fixture()
def client():
    from agentix.main import application

    with TestClient(application) as c:
        yield c


def _scrape(client: TestClient):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    return resp.text


def test_metrics_endpoint_scrapable(client):
    """端点可被抓取：产生一次请求计数后，指标族应出现在文本中。"""
    record_request("GET", "/metrics-test", 200, 0.01)
    body = _scrape(client)
    # 降级环境（prometheus_client 缺失）时正文可能为空提示文本；
    # 本仓 prometheus_client 为 requirements 依赖，正常环境应含指标。
    if metrics_module.PROMETHEUS_AVAILABLE:
        assert "http_requests_total" in body


def test_metrics_endpoint_graceful_degradation(client, monkeypatch):
    """prometheus_client 不可用时返回空正文 200，而不是 5xx。"""
    monkeypatch.setattr(metrics_module, "PROMETHEUS_AVAILABLE", False)
    monkeypatch.setattr(metrics_module, "get_metrics", lambda: b"")
    body = _scrape(client)
    assert body == ""


def test_metrics_exempt_from_api_key(client, monkeypatch):
    """REQUIRE_API_KEY=true 时 /metrics 仍可被抓取（监控面免鉴权）。"""
    from agentix.config import settings as unified_settings

    monkeypatch.setattr(unified_settings, "REQUIRE_API_KEY", True)
    monkeypatch.setattr(unified_settings, "API_KEY", "test-secret-key")
    # 不带 x-api-key 的抓取应放行（/metrics 在 EXEMPT_PATHS）
    resp = client.get("/metrics")
    assert resp.status_code == 200
    # 对照组：非白名单路径仍被拦截
    resp2 = client.get("/api/v1/chat/routes")
    assert resp2.status_code == 401


# ---------------------------------------------------------------------------
# 企业化 P0-4：OTel 硬依赖断言
# ---------------------------------------------------------------------------


def test_opentelemetry_is_hard_dependency():
    """OTel 已在 requirements.txt 钉版，任何按 requirements 安装的环境都必须可用。

    此前为软依赖：未安装时静默降级为 noop tracer（评估报告 §3 实证的
    "生产可能静默禁用追踪"）。转硬依赖后此测试成为 CI 断言——缺失即红。
    """
    from agentix.observability import tracing as tracing_module

    assert tracing_module.OTEL_AVAILABLE is True, (
        "opentelemetry-sdk / exporter-otlp 未安装或导入失败——"
        "P0-4 起为硬依赖，请核对 requirements.txt 并重新安装"
    )


def test_init_tracing_produces_real_tracer():
    """init_tracing 后应产出真实 OTel tracer（而非 noop），可构建 span。"""
    from agentix.observability import tracing as tracing_module

    if not tracing_module.OTEL_AVAILABLE:
        pytest.skip("OTel 不可用（硬依赖断言应已在上一用例拦截）")

    tracing_module.init_tracing(service_name="agentix-test", console_export=False)
    # tracer 已替换为 OTel ProxyTracer（不再是 _NoopTracer）
    assert not isinstance(tracing_module.tracer, tracing_module._NoopTracer)
    # console_export=False 且未配置 OTLP 端点 → 无网络导出，span 构建无副作用
    # trace_span 为 async 上下文管理器，同步冒烟用 sync_trace_span
    with tracing_module.sync_trace_span("p0_4_smoke") as span:
        span.set_attribute("test", "value")
