"""P37：HTTP 层离线端到端（P2-2「核心链路单测，脱离外部服务」的收口）。

**为什么需要它**：P2-2 指出 `test_chat_api.py` / `test_router_smoke.py`
依赖 `localhost:8000`，"CI 不可运行，重构无安全网"。批次 13 复核时发现更糟的情况：

- `test_chat_api.py` 的两个用例**既没有 `integration` 标记、也没有 `asyncio` 标记**
  → 被 pytest 收集后**协程从未被 await，静默不执行**，给人「端到端已有覆盖」的假象；
- `test_router_smoke.py` **完全没有 `test_*` 函数**（纯手动脚本），
  却躺在 `tests/` 里被 pytest 导入。

本文件用 **FastAPI TestClient** 提供真正的**离线** HTTP 层覆盖：不连 Redis / MySQL /
Neo4j / Milvus 也能验证路由装配、中间件、异常处理与安全防护。

**关于外部服务**：无外部服务时，依赖它们的端点（ops / sessions 等）会由
`global_exception_handler` 收敛为 500 —— 这正是要断言的行为
（**异常不外泄、也不让进程崩**），而非测试的失败。

测试较慢（应用启动约 15–20s），故用 **module 级** client 复用。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.slow

# 应用启动较慢（会尝试连接 Redis / Neo4j 后降级），放宽超时
CLIENT_TIMEOUT = 120


@pytest.fixture(scope="module")
def client():
    """离线 TestClient（**不进入 lifespan**）。

    两个刻意的选择：

    1. **不用 `with`**：进入上下文会触发 `startup_event`，其中的
       `Base.metadata.create_all()` 在无 MySQL 时**直接抛异常**，
       整个 fixture 就挂了。本套测试关注的是**路由装配 / 中间件 / 异常处理**，
       不需要启动期的副作用。
    2. ``raise_server_exceptions=False``：让 `global_exception_handler`
       正常返回 500，而不是把异常抛进测试进程——这样才能断言
       「外部服务不可用时**优雅 500**」这一契约。
    """
    from starlette.testclient import TestClient

    import agentix.main as main_module

    return TestClient(main_module.application, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# 应用装配与健康检查
# ---------------------------------------------------------------------------


class TestAppBootstrap:
    def test_health_endpoint_responds(self, client):
        resp = client.get("/health", timeout=CLIENT_TIMEOUT)
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "services" in data

    def test_health_reports_package_version(self, client):
        """健康检查的版本必须来自包（单一事实来源），不得是过期常量。"""
        import agentix

        data = client.get("/health", timeout=CLIENT_TIMEOUT).json()
        assert data["version"] == agentix.__version__

    def test_root_api_endpoint(self, client):
        resp = client.get("/api", timeout=CLIENT_TIMEOUT)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "My Agentix"

    def test_chat_routes_are_exposed(self, client):
        """路由清单端点应返回已注册的路由类型（供前端展示）。"""
        resp = client.get("/api/v1/chat/routes", timeout=CLIENT_TIMEOUT)
        assert resp.status_code == 200
        routes = resp.json()["routes"]
        assert isinstance(routes, dict) and routes
        assert "general-query" in routes

    def test_legal_knowledge_router_is_mounted(self, client):
        """R1.x 融合后法律知识库能力由 retrieval 域路由承载（/api/v1/retrieval/*）。"""
        from route_introspect import iter_effective_routes

        paths = [
            getattr(r, "path", "")
            for r in iter_effective_routes(client.app.routes)  # type: ignore[attr-defined]
        ]
        assert any(p.startswith("/api/v1/retrieval") for p in paths), (
            "retrieval 域路由（知识库能力承载方）未挂载"
        )


# ---------------------------------------------------------------------------
# 中间件与异常收敛
# ---------------------------------------------------------------------------


class TestMiddlewareAndErrorHandling:
    def test_trace_id_header_is_present(self, client):
        """P0-4：trace_id 中间件应在响应头回显（全链路可关联）。"""
        resp = client.get("/health", timeout=CLIENT_TIMEOUT)
        assert "x-trace-id" in resp.headers
        assert resp.headers["x-trace-id"]

    def test_explicit_trace_id_is_echoed(self, client):
        resp = client.get(
            "/health", headers={"X-Trace-Id": "test-trace-123"}, timeout=CLIENT_TIMEOUT
        )
        assert resp.headers["x-trace-id"] == "test-trace-123"

    def test_unknown_path_returns_404_json(self, client):
        resp = client.get("/api/v1/definitely-not-a-route", timeout=CLIENT_TIMEOUT)
        assert resp.status_code == 404
        assert "detail" in resp.json()

    def test_redis_down_collapses_to_500(self, client, monkeypatch):
        """Redis 不可用时，依赖它的端点应返回 **500 而非让进程崩**。

        这是 `global_exception_handler` 的验收：异常被收敛、响应体不带堆栈也不带
        原始异常文案（处理器只回通用文案，见 `agentix/main.py::global_exception_handler`）。

        故障是**注入**的而不是等环境自然发生：宿主机上恰好跑着 Redis 时，
        `ops/dashboard` 会正常返回 200，这条用例就会从「量异常收敛」退化成
        「量本机装没装 Redis」——本地绿、CI 红，取决于环境而不取决于代码。
        """
        import redis

        from agentix.interfaces.http import ops_router

        class _ObservabilityWithRedisDown:
            """门面替身：取一次看板数据就抛连接错误，等价于 Redis 掉线。"""

            async def get_dashboard(self):
                raise redis.exceptions.ConnectionError("p37: injected redis outage")

        monkeypatch.setattr(ops_router, "get_observability", lambda: _ObservabilityWithRedisDown())

        resp = client.get("/api/v1/ops/dashboard", timeout=CLIENT_TIMEOUT)
        assert resp.status_code == 500, f"Redis 掉线未收敛为 500：{resp.status_code}"
        assert "error" in resp.json()
        assert "Traceback" not in resp.text
        assert "injected redis outage" not in resp.text

    def test_database_down_collapses_to_500(self, client):
        """DB 不可用时，sessions 端点应返回 **500 而非让进程崩**。

        同样用注入替代「等环境里没有 MySQL」：DB 可达与否取决于宿主 `DATABASE_URL`
        指向哪个库（CI 指 SQLite，本地 `.env` 指 compose 里的 PostgreSQL），
        真实调用等于让用例的结论随宿主配置漂移。这里把 `get_db` 依赖换成
        「一取连接就抛 OperationalError」，直接量 DB 层的异常是否走同一个收敛路径。
        （sessions 的 user_id 已改必填——缺参只会 422，须带参才能进到 DB 层。）
        """
        from sqlalchemy.exc import OperationalError

        from agentix.infrastructure.core.database import get_db

        def _unreachable_session():
            raise OperationalError(
                "SELECT 1", {}, Exception("p37: injected db outage")
            )

        client.app.dependency_overrides[get_db] = _unreachable_session
        try:
            resp = client.get("/api/v1/sessions/?user_id=u-p37", timeout=CLIENT_TIMEOUT)
        finally:
            client.app.dependency_overrides.pop(get_db, None)

        assert resp.status_code == 500, f"DB 掉线未收敛为 500：{resp.status_code}"
        assert "error" in resp.json()
        assert "Traceback" not in resp.text
        assert "injected db outage" not in resp.text


# ---------------------------------------------------------------------------
# 安全：上传路径遍历
# ---------------------------------------------------------------------------


class TestUploadPathTraversal:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/upload/files/..%2f..%2f.env",
            "/api/v1/upload/images/../../etc/passwd",
            "/api/v1/upload/files/%2e%2e%2f.env",
        ],
    )
    def test_traversal_attempts_never_succeed(self, client, path: str):
        """路径遍历必须被拦下——绝不能返回 200 + 文件内容。"""
        resp = client.get(path, timeout=CLIENT_TIMEOUT)
        assert resp.status_code != 200, f"路径遍历未被拦截：{path} -> 200"
        # 即便响应体非空，也不得含敏感内容
        assert "SECRET_KEY" not in resp.text
        assert "API_KEY=" not in resp.text
