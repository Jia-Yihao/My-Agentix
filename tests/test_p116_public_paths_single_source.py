"""P116：公开路径清单的单一事实源（G7 卫生项收口）。

案发背景：`APIKeyMiddleware.EXEMPT_PATHS` 与 `GlobalRateLimitMiddleware._EXEMPT_PATHS`
是两份手抄的集合，注释只说"同口径"。加一个键只改一处，两套闸门就会在同一个 URL 上
给出相反答案——本仓在 p115（配置矩阵）已经吃过同一种病：**两处各写一份判据，迟早只改一处**。

本批次做两件事：
1. 清单收进 `agentix/interfaces/http/public_paths.py`，两个中间件 import 同一对象；
2. 删掉 `/api` 的公开豁免——它是根信息端点（应用名/版本号），不是探针。`REQUIRE_API_KEY`
   开着时没有理由匿名放行；探针是 `/health`（compose healthcheck 与 Vite proxy 用）。

`/api` 从来不是鉴权绕过：匹配一直是精确成员判断，`/api/v1/*` 照旧 401。这一点在本文件里
被**反向钉死**——`/api/v1/chat/routes` 必须仍是 401，免得删掉 `/api` 时顺手把匹配改成前缀。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentix.config import settings
from agentix.interfaces.http import rate_limit_middleware
from agentix.interfaces.http.public_paths import PUBLIC_PATHS, PUBLIC_PREFIXES
from agentix.interfaces.http.rate_limit_middleware import GlobalRateLimitMiddleware

KEY = "p116-sentinel-key"


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/api")
    def root():
        return {"name": "probe", "status": "running"}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/liveness")
    def liveness():
        return {"status": "alive"}

    @app.get("/readiness")
    def readiness():
        return {"status": "ready"}

    @app.get("/redoc")
    def redoc():
        return {"redoc": True}

    @app.get("/favicon.ico")
    def favicon():
        return {"icon": True}

    @app.get("/metrics")
    def metrics():
        return {"m": 1}

    @app.get("/openapi.json")
    def openapi():
        return {"openapi": "3.0.0"}

    @app.get("/docs")
    def docs():
        return {"docs": True}

    @app.get("/api/v1/chat/routes")
    def routes():
        return {"routes": {}}

    return app


def _client_with_auth() -> TestClient:
    from agentix.main import APIKeyMiddleware

    app = _app()
    app.add_middleware(APIKeyMiddleware)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_on(monkeypatch):
    monkeypatch.setattr(settings, "REQUIRE_API_KEY", True)
    monkeypatch.setattr(settings, "API_KEY", KEY)


class TestSingleSource:
    def test_public_paths_are_exactly_the_probes_docs_and_metrics(self):
        # 精确集合 = 反向钉子：往公开面加任何东西都必须先改这里。
        assert PUBLIC_PATHS == frozenset(
            {
                "/docs",
                "/redoc",
                "/health",
                "/liveness",
                "/readiness",
                "/favicon.ico",
                "/openapi.json",
                "/metrics",
            }
        )

    def test_root_api_is_deliberately_absent(self):
        assert "/api" not in PUBLIC_PATHS

    def test_prefixes_are_docs_only(self):
        assert PUBLIC_PREFIXES == ("/docs", "/redoc")

    def test_the_set_is_immutable(self):
        with pytest.raises(AttributeError):
            PUBLIC_PATHS.add("/api")  # type: ignore[attr-defined]

    def test_auth_middleware_uses_the_canonical_object(self):
        from agentix.main import APIKeyMiddleware

        assert APIKeyMiddleware.EXEMPT_PATHS is PUBLIC_PATHS

    def test_auth_middleware_judges_with_the_canonical_names(self):
        """类属性指过去还不够：`__call__` 里若另抄一份字面量，行为今天一样、机制已经分家。
        变异测试实测——只断言身份时，"本地再抄一份"的变体 30 条用例全绿。"""
        import inspect

        from agentix.main import APIKeyMiddleware

        source = inspect.getsource(APIKeyMiddleware.__call__)
        assert "path in PUBLIC_PATHS" in source
        assert "path.startswith(PUBLIC_PREFIXES)" in source
        assert '"/health"' not in source and '"/docs"' not in source

    def test_rate_limit_middleware_uses_the_canonical_object(self):
        # 旧的两份手抄集合必须已删除——只要还能找到 `_EXEMPT_PATHS`，
        # 就说明有一条路可以只改一处而不改另一处。
        assert not hasattr(rate_limit_middleware, "_EXEMPT_PATHS")
        assert not hasattr(rate_limit_middleware, "_EXEMPT_PREFIXES")
        assert rate_limit_middleware.PUBLIC_PATHS is PUBLIC_PATHS
        assert rate_limit_middleware.PUBLIC_PREFIXES is PUBLIC_PREFIXES


class TestAuthMiddlewareBehaviour:
    def test_root_api_is_protected_when_auth_is_on(self, auth_on):
        response = _client_with_auth().get("/api")
        assert response.status_code == 401
        assert "probe" not in response.text, "401 响应体不得带出被保护端点的内容"

    def test_root_api_is_reachable_with_a_valid_key(self, auth_on):
        response = _client_with_auth().get("/api", headers={"x-api-key": KEY})
        assert response.status_code == 200
        assert response.json()["status"] == "running"

    @pytest.mark.parametrize("path", sorted(PUBLIC_PATHS))
    def test_public_paths_pass_without_a_key(self, auth_on, path):
        response = _client_with_auth().get(path)
        assert response.status_code == 200, f"{path} 必须保持公开，实际 {response.status_code}"

    def test_docs_prefix_passes_through(self, auth_on):
        # 前缀豁免仍然生效：未定义的子路径应由应用回 404，而不是被鉴权拦成 401。
        assert _client_with_auth().get("/docs/oauth2-redirect").status_code != 401

    def test_api_prefix_is_not_a_bypass(self, auth_on):
        assert _client_with_auth().get("/api/v1/chat/routes").status_code == 401

    @pytest.mark.parametrize("path", ["/api/v1/chat/routes", "/api/v1/healthz"])
    def test_paths_that_merely_contain_a_public_name_are_protected(self, auth_on, path):
        # 精确成员判断：`/api/v1/healthz` 只是**包含** "/health"，不是探针本身。
        # 这同时钉住"别把 in 改成子串匹配"——那种改法会把它放进来。
        assert _client_with_auth().get(path).status_code == 401

    def test_auth_off_keeps_root_api_reachable(self, monkeypatch):
        monkeypatch.setattr(settings, "REQUIRE_API_KEY", False)
        assert _client_with_auth().get("/api").status_code == 200


class TestRateLimitMiddlewaresAgrees:
    @pytest.mark.parametrize("path", sorted(PUBLIC_PATHS))
    def test_every_public_path_is_rate_limit_exempt(self, path):
        assert GlobalRateLimitMiddleware._is_exempt(path) is True

    @pytest.mark.parametrize(
        "path",
        ["/api", "/api/v1/chat/routes", "/api/v1/healthz", "/metrics/extra", "/v1/chat"],
    )
    def test_non_public_paths_are_rate_limited(self, path):
        assert GlobalRateLimitMiddleware._is_exempt(path) is False
