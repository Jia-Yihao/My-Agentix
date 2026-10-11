"""企业化 P0-3：全局限流中间件验证。

验收（评估报告 §4.1）：
1. 超过桶容量后返回 429 + Retry-After，并打 rate_limit_triggered_total{scope="global"}；
2. 豁免路径（/health、/metrics、/docs 等）不受限流影响；
3. 不同客户端 IP 相互隔离；
4. enabled=False 时全部放行（灰度开关）；
5. X-Forwarded-For 首跳生效（反代场景）。

测试用独立的迷你 FastAPI 应用（不加载完整 agentix.main，隔离且快）。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentix.interfaces.http.rate_limit_middleware import (
    GlobalRateLimitMiddleware,
    _client_ip,
)


def _build_app(rpm: int, burst: int, enabled: bool = True) -> FastAPI:
    app = FastAPI()
    app.add_middleware(GlobalRateLimitMiddleware, rpm=rpm, burst=burst, enabled=enabled)

    @app.get("/hello")
    async def hello():
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics():
        return {"metrics": True}

    return app


def test_limit_exceeded_returns_429_with_retry_after():
    app = _build_app(rpm=60, burst=2)  # 桶容量 2
    with TestClient(app) as client:
        assert client.get("/hello").status_code == 200
        assert client.get("/hello").status_code == 200
        resp = client.get("/hello")
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
        assert resp.json()["error"] == "Too Many Requests"


def test_exempt_paths_never_limited():
    app = _build_app(rpm=60, burst=1)
    with TestClient(app) as client:
        # 耗尽普通路径配额
        assert client.get("/hello").status_code == 200
        assert client.get("/hello").status_code == 429
        # 豁免路径仍放行
        for _ in range(5):
            assert client.get("/health").status_code == 200
            assert client.get("/metrics").status_code == 200
            assert client.get("/docs").status_code in (200, 404)


def test_client_ip_isolation_via_forwarded_for():
    app = _build_app(rpm=60, burst=1)
    with TestClient(app) as client:
        h1 = {"X-Forwarded-For": "10.0.0.1"}
        h2 = {"X-Forwarded-For": "10.0.0.2"}
        assert client.get("/hello", headers=h1).status_code == 200
        assert client.get("/hello", headers=h1).status_code == 429
        # 另一个 IP 不受影响
        assert client.get("/hello", headers=h2).status_code == 200


def test_disabled_switch_passes_all():
    app = _build_app(rpm=60, burst=1, enabled=False)
    with TestClient(app) as client:
        for _ in range(10):
            assert client.get("/hello").status_code == 200


def test_client_ip_extraction_prefers_forwarded_for():
    scope = {"headers": [(b"x-forwarded-for", b"1.2.3.4, 5.6.7.8")], "client": ("9.9.9.9", 1000)}
    assert _client_ip(scope) == "1.2.3.4"
    scope2 = {"headers": [], "client": ("9.9.9.9", 1000)}
    assert _client_ip(scope2) == "9.9.9.9"


def test_rate_limit_metric_incremented():
    from agentix.observability import metrics as m

    app = _build_app(rpm=60, burst=1)
    with TestClient(app) as client:
        before = _read_counter(m, "global")
        client.get("/hello")
        client.get("/hello")  # 第 2 次：429
        after = _read_counter(m, "global")
        assert after == before + 1


def _read_counter(m, scope: str) -> float:
    if not m.PROMETHEUS_AVAILABLE:
        return 0.0
    value = m.RATE_LIMIT_TRIGGERED.labels(scope=scope)._value.get()
    return float(value)


# ===========================================================================
# 审计批次 A3（2026-09-28）：rate_limit_dependency 不写共享 config、
# per-params 派生实例；retrieval chat 限流器热重建
# ===========================================================================


def _stub_request(path: str = "/probe"):
    from types import SimpleNamespace

    req = SimpleNamespace()
    req.client = SimpleNamespace(host="10.1.2.3")
    req.url = SimpleNamespace(path=path)
    return req


class _FakeLimiter:
    def __init__(self, allowed: bool = True, remaining: float = 5.0):
        self.allowed = allowed
        self.remaining = remaining
        self.keys: list[str] = []

    async def acquire(self, key: str) -> bool:
        self.keys.append(key)
        return self.allowed

    async def get_remaining(self, key: str) -> float:
        return self.remaining


def test_dependency_uses_per_params_limiter_never_shared_global(monkeypatch):
    """A3：依赖走 per-params 实例，绝不再摸全局共享 limiter 的 config。"""
    import asyncio

    from agentix.infrastructure.core import ratelimit as rl

    monkeypatch.setattr(rl.settings, "RATE_LIMIT_ENABLED", True)
    # 隔离缓存（测试后还原）
    monkeypatch.setattr(rl, "_param_limiter_cache", dict(rl._param_limiter_cache))
    # 共享 limiter 一旦被取用即失败（原实现：get_rate_limiter() + 改 config）
    monkeypatch.setattr(
        rl, "get_rate_limiter", lambda: pytest.fail("依赖不得触共享 limiter")
    )
    before = rl._rate_limiter_instance

    fake = _FakeLimiter()
    monkeypatch.setitem(rl._param_limiter_cache, (7, 7), fake)

    assert asyncio.run(rl.rate_limit_dependency(_stub_request(), 7, 7)) is None
    assert fake.keys and fake.keys[0] == "endpoint:/probe:10.1.2.3"
    assert rl._rate_limiter_instance is before, "全局共享实例不得被依赖触碰"


def test_dependency_429_carries_limit_headers(monkeypatch):
    import asyncio

    from fastapi import HTTPException

    from agentix.infrastructure.core import ratelimit as rl

    monkeypatch.setattr(rl.settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(rl, "_param_limiter_cache", dict(rl._param_limiter_cache))
    fake = _FakeLimiter(allowed=False, remaining=1.0)
    monkeypatch.setitem(rl._param_limiter_cache, (3, 3), fake)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(rl.rate_limit_dependency(_stub_request(), 3, 3))
    assert excinfo.value.status_code == 429
    assert excinfo.value.headers["Retry-After"] == "3"
    assert excinfo.value.headers["X-RateLimit-Limit"] == "3"


def test_limiter_for_derives_isolated_instances_per_params():
    """A3：不同参数 → 不同实例互不干扰；同参数复用且配置按参数派生。"""
    from agentix.infrastructure.core import ratelimit as rl

    cache_snapshot = dict(rl._param_limiter_cache)
    try:
        rl._param_limiter_cache.clear()
        a = rl._limiter_for(5, 10)
        a2 = rl._limiter_for(5, 10)
        b = rl._limiter_for(30, 60)
        assert a is a2, "同参数必须复用同一实例"
        assert a is not b, "不同参数必须各自独立（原共享 config 会互踩）"
        assert a.config.max_tokens == 5
        assert a.config.refill_rate == 0.5
        assert b.config.max_tokens == 30
        # 原实现的落点：改写共享实例 config —— 现在不可能发生
        assert rl._rate_limiter_instance is None or rl._rate_limiter_instance.config is not a.config
    finally:
        rl._param_limiter_cache.clear()
        rl._param_limiter_cache.update(cache_snapshot)


def test_retrieval_chat_limiter_hot_rebuild_on_settings_change(monkeypatch):
    """A3：retrieval chat 限流器不再 import 期绑定——参数变化热重建。"""
    from agentix.retrieval.api.routers import chat as rchat

    monkeypatch.setattr(rchat, "_chat_limiter", None)
    monkeypatch.setattr(rchat, "_chat_limiter_signature", None)

    first = rchat._get_chat_limiter()
    assert rchat._get_chat_limiter() is first, "同签名必须复用"

    monkeypatch.setattr(rchat.settings, "RATE_LIMIT_RATE", first.rate + 7)
    rebuilt = rchat._get_chat_limiter()
    assert rebuilt is not first, "参数变化必须热重建（原 import 期绑定改不动）"
    assert rchat._chat_limiter_signature == (
        rchat.settings.RATE_LIMIT_RATE,
        rchat.settings.RATE_LIMIT_BURST,
    )
    assert rebuilt.rate == rchat.settings.RATE_LIMIT_RATE
