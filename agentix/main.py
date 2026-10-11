"""
My Agentix FastAPI application entry point.
"""

import asyncio
import os
import re
import secrets
import time
import uvicorn
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from agentix.application.services.lightrag_service import get_lightrag_service
from agentix.config import settings
from agentix.infrastructure.core import configure_logging
from agentix.infrastructure.core.database import Base, engine
from agentix.interfaces.http import lightrag_router
from agentix.interfaces.http.v1 import api_router as api_v1_router
from agentix.retrieval.api.routers import router as retrieval_router
from agentix.retrieval.errors import AppError
from agentix.retrieval.api.response import ERROR_CODE_TO_STATUS, error as retrieval_error_body

# Configure logging before app creation
configure_logging(debug=settings.DEBUG)

# Application instance --------------------------------------------------------
# 企业化 P2-1.1：lifespan 迁移——on_event("startup"/"shutdown") 已弃用，
# 改为 FastAPI 官方推荐的 lifespan 协议。_startup/_shutdown 的函数体在
# 模块后文定义（lifespan 闭包只在应用启动/退出时才执行，届时已可解析）。
@asynccontextmanager
async def lifespan(_app: FastAPI):
    await _startup()
    yield
    await _shutdown()


application = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="My Agentix - production-oriented Agent platform powered by multi-agent architecture",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Global middleware -----------------------------------------------------------
application.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# API Key authentication middleware --------------------------------------------
class APIKeyMiddleware:
    """Optional API key authentication middleware.

    Enabled via REQUIRE_API_KEY=true in settings.
    Exempts /docs, /redoc, /health, /favicon.ico from auth.
    """

    EXEMPT_PATHS = {"/docs", "/redoc", "/health", "/liveness", "/readiness", "/favicon.ico", "/api", "/openapi.json", "/metrics"}

    def __init__(self, app):
        self.app = app
        self._warned_disabled = False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        if not settings.REQUIRE_API_KEY:
            # P0-6 warn 模式：鉴权关闭时显式告警（只提示，不拦截），便于观察期审计。
            # 只在进程内告警一次——原先每个请求打一条，高 QPS 下会刷爆日志。
            if not self._warned_disabled:
                self._warned_disabled = True
                logger.warning(
                    "API key auth is DISABLED (REQUIRE_API_KEY=False); "
                    "all endpoints are public. Enable before production."
                )
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if path in self.EXEMPT_PATHS or path.startswith("/docs") or path.startswith("/redoc"):
            return await self.app(scope, receive, send)

        headers = dict(scope.get("headers", []))
        api_key = headers.get(b"x-api-key", b"").decode("utf-8")

        # P0-6：恒定时间比较，避免时序侧信道泄露密钥前缀
        if not api_key or not settings.API_KEY or not secrets.compare_digest(api_key, settings.API_KEY):
            response = JSONResponse(
                status_code=401,
                content={"error": "Unauthorized", "message": "Invalid or missing API key"},
            )
            return await response(scope, receive, send)

        return await self.app(scope, receive, send)


application.add_middleware(APIKeyMiddleware)


# Request logging middleware ---------------------------------------------------
# Phase H · W0：endpoint 标签归一化——动态段（数字/UUID/长十六进制）收敛为 {id}，
# 段数封顶，控制 Prometheus request 指标的基数。
_ENDPOINT_SEGMENT_RE = re.compile(
    r"^(?:[0-9]+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32})$",
    re.IGNORECASE,
)
_MAX_ENDPOINT_SEGMENTS = 6


def _normalize_endpoint(path: str) -> str:
    """把请求路径归一化为低基数 endpoint 标签（`/v1/users/123` → `/v1/users/{id}`）。"""
    if not path or path == "/":
        return "/"
    parts = [p for p in path.split("/") if p]
    normalized = []
    for i, raw in enumerate(parts):
        if i >= _MAX_ENDPOINT_SEGMENTS:
            normalized.append("*")
            break
        seg = raw.split("?", 1)[0]  # 兜底：path 理论上不含 query，防御性剥离
        if len(seg) >= 24 or _ENDPOINT_SEGMENT_RE.match(seg):
            normalized.append("{id}")
        else:
            normalized.append(seg)
    return "/" + "/".join(normalized)


class RequestLoggingMiddleware:
    """Logs request method, path, status code and processing time."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        start = time.time()
        path = scope.get("path", "")

        status_code = 500

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 500)
            await send(message)

        await self.app(scope, receive, send_wrapper)

        elapsed = time.time() - start
        client = scope.get("client")
        client_host = client[0] if client else "unknown"
        logger.info(
            "{} {} {} {} {:.3f}s",
            client_host,
            scope.get("method", "?"),
            path,
            status_code,
            elapsed,
        )
        # Phase H · W0：Prometheus 请求指标接线（prometheus_client 缺失时为 no-op）。
        try:
            from agentix.observability.metrics import record_request

            record_request(
                method=scope.get("method", "?"),
                endpoint=_normalize_endpoint(path),
                status=status_code,
                duration=elapsed,
            )
        except Exception:  # noqa: BLE001 - 指标失败绝不影响请求日志与响应
            pass


application.add_middleware(RequestLoggingMiddleware)

# P0-4：trace_id 贯穿中间件——放在最外层（Starlette 后 add 先执行），
# 使请求日志/鉴权/Agent/LLM 埋点都能读到同一 trace_id。
from agentix.interfaces.http.middleware import TraceIdMiddleware  # noqa: E402

application.add_middleware(TraceIdMiddleware)

# 企业化 P0-3：全局限流中间件——置于 trace_id 外层（最先执行，先于鉴权/
# 日志 shed load）。按客户端 IP 令牌桶限流，覆盖法律编排域 + 检索域全部入口；
# 知识域聊天路由另有更细粒度的路由级限流（retrieval/settings.py RATE_LIMIT_*）。
# 多副本部署时应切 RedisRateLimiter（见 rate_limit_middleware 模块注释）。
from agentix.interfaces.http.rate_limit_middleware import GlobalRateLimitMiddleware  # noqa: E402

application.add_middleware(
    GlobalRateLimitMiddleware,
    rpm=settings.RATE_LIMIT_GLOBAL_RPM,
    burst=settings.RATE_LIMIT_GLOBAL_BURST,
    enabled=settings.RATE_LIMIT_ENABLED,
    # R1-4（指南 §2.1.5②）：上传接口单独配额（重 IO+触发索引，配额更紧）
    upload_rpm=settings.UPLOAD_RATE_LIMIT_RPM,
    upload_burst=settings.UPLOAD_RATE_LIMIT_BURST,
)

# Routers --------------------------------------------------------------------
application.include_router(lightrag_router.router, prefix=settings.API_V1_PREFIX)
application.include_router(api_v1_router, prefix=settings.API_V1_PREFIX)
application.include_router(retrieval_router, prefix=settings.API_V1_PREFIX + "/retrieval")

from agentix.interfaces.http.ops_router import router as ops_router
application.include_router(ops_router, prefix=settings.API_V1_PREFIX)

# Static files ---------------------------------------------------------------
# 创建上传目录。
# 必须与写入侧（interfaces/http/v1/upload.py 的 UPLOAD_DIR）同源：
# 原先硬编码 agentix/../../uploads，当进程 CWD ≠ 仓库根（docker/systemd）时，
# 文件写到 A 目录、静态服务却读 B 目录，上传后 URL 404。
uploads_dir = str(Path(settings.UPLOAD_DIR).resolve())
os.makedirs(uploads_dir, exist_ok=True)

# 挂载静态文件
application.mount("/uploads", StaticFiles(directory=uploads_dir), name="uploads")

@application.get("/")
async def root_redirect():
    """Redirect to API docs.

    The web UI is a separate Vite app under `web/` and is typically run via `npm run dev`.
    """
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/docs")


@application.get("/favicon.ico")
async def favicon():
    """Return an empty favicon to avoid noisy 404s in dev."""
    from fastapi.responses import Response
    return Response(content=b"", media_type="image/x-icon")


@application.get("/api")
async def root() -> dict:
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "status": "running",
        "docs": "/docs",
    }


@application.get("/health")
async def health_check() -> dict:
    """Health check endpoint that verifies connectivity to key services."""
    services = {}

    # Check Redis
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.REDIS_URL, socket_timeout=2)
        await r.ping()
        await r.aclose()
        services["redis"] = "healthy"
    except Exception as e:
        services["redis"] = f"unhealthy: {type(e).__name__}"

    # Check Neo4j
    try:
        from agentix.application.agents.kg_sub_graph.kg_neo4j_conn import get_neo4j_graph
        graph = get_neo4j_graph()
        if graph:
            with graph.driver.session() as session:
                session.run("RETURN 1")
            services["neo4j"] = "healthy"
        else:
            services["neo4j"] = "unhealthy: no graph instance"
    except Exception as e:
        services["neo4j"] = f"unhealthy: {type(e).__name__}"

    # Check Milvus
    try:
        from pymilvus import connections as milvus_connections
        milvus_connections.connect(alias="health_check", host=settings.MILVUS_HOST, port=settings.MILVUS_PORT)
        from pymilvus import utility
        utility.list_collections(using="health_check")
        milvus_connections.disconnect("health_check")
        services["milvus"] = "healthy"
    except Exception as e:
        services["milvus"] = f"unhealthy: {type(e).__name__}"

    all_healthy = all(v == "healthy" for v in services.values())
    return {
        "status": "healthy" if all_healthy else "degraded",
        "version": settings.APP_VERSION,
        "services": services,
    }


@application.get("/liveness")
async def liveness_check() -> dict:
    """Liveness probe — process is alive and can serve requests."""
    return {"status": "alive", "version": settings.APP_VERSION}


@application.get("/readiness")
async def readiness_check() -> dict:
    """Readiness probe — dependencies are reachable and service can handle traffic."""
    return await health_check()


# Prometheus 指标暴露（P0-1）---------------------------------------------------
# 供 Prometheus 抓取：observability/metrics.py 的全部 Counter/Histogram/Gauge。
# prometheus_client 未安装时降级为空正文（抓取不报错，但应在生产环境安装）。
from fastapi.responses import Response  # noqa: E402
from agentix.observability.metrics import get_metrics, get_metrics_content_type  # noqa: E402


@application.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    """Prometheus scrape endpoint（免鉴权，见 APIKeyMiddleware.EXEMPT_PATHS）。"""
    return Response(content=get_metrics(), media_type=get_metrics_content_type())


def _run_settings_validation() -> None:
    """执行检索域的生产配置校验，把结果变成**启动日志**，生产环境则**拒绝启动**。

    语义（三态，与"自检不得成为新的故障源"的原则一致）：

    ==================  ==========================  ========================
    校验结果            开发环境                    生产环境（PRODUCTION）
    ==================  ==========================  ========================
    无 error            记一条 info（含告警条数）    同左
    有 warning          逐条 warning（不阻断）       同左
    有 error            逐条 error（**不阻断**）     逐条 error + **raise**
    ==================  ==========================  ========================

    为什么生产环境要拒绝启动：这些 error 对应的都是"能被静默容忍、但会导致错误行为"的
    配置（典型：milvus + 未归一化 ⇒ 相似度口径错，但接口仍返回 200 和一堆结果）。
    **让它带着错误语义对外服务，比让它启动时就响亮地失败要危险得多。**

    为什么开发环境不阻断：本地跑测试/调试时常有意关掉限流或输出过滤，
    此时阻断会把"校验"变成新的故障源——但也必须留下日志，否则又回到"静默"。
    """
    from agentix.retrieval.settings import settings as _rs
    from agentix.retrieval.settings import validate_app_settings

    errors, warnings = validate_app_settings()
    for w in warnings:
        logger.warning("settings validation (warning): {}", w)
    if not errors:
        logger.info(
            "settings validation: passed ({} warning(s), env={})",
            len(warnings),
            _rs.ENVIRONMENT,
        )
        return
    for e in errors:
        logger.error("settings validation (error): {}", e)
    if _rs.PRODUCTION:
        raise RuntimeError(
            "refusing to start: {} settings validation error(s) in production "
            "(env={}): {}".format(len(errors), _rs.ENVIRONMENT, "; ".join(errors))
        )


def _build_cron_runner():
    """P5：cron 任务默认执行体（**组合根定义**——分层守则：runner 要开 DB 会话、
    走 chat_service / ToolExecutor，属 composition root 职责，application 层不
    import infrastructure）。

    - ``kind="tool"``：经 ``ToolExecutor.execute`` 跑（风险/权限/HITL 全闸门
      不旁路——出站即副作用）；
    - ``kind="agent"``：每轮**独立会话**跑一遍 ``chat_service.ask``（定时任务
      是一次性 turn，会话隔离 = 任务互不串上下文；长期记忆经既有记忆管线延续）。
    """

    async def _runner(job) -> str:
        if job.kind == "tool":
            from agentix.tools.gateway.executor import get_tool_executor

            result = await get_tool_executor().execute(job.tool, dict(job.args or {}))
            if not result.success:
                raise RuntimeError(result.error or f"tool {job.tool} failed")
            data = result.data
            return (data if isinstance(data, str) else str(data))[:2000]
        from agentix.application.services.chat_service import get_chat_service
        from agentix.infrastructure.core.database import SessionLocal

        db = SessionLocal()
        try:
            service = get_chat_service()
            session_id = service.get_or_create_session(db, None, "cron_scheduler")
            outcome = await service.ask(job.prompt, session_id)
            return str(outcome.get("message") or outcome.get("status") or "")[:2000]
        finally:
            db.close()

    return _runner


# Phase H · W0：事件循环滞后采样任务句柄（flag 默认关，不启动即无任何开销）。
_lag_sampler_task: asyncio.Task | None = None


async def _event_loop_lag_sampler() -> None:
    """后台采样事件循环调度滞后（perf_counter 期望唤醒时刻 vs 实际唤醒）。"""
    from agentix.observability.metrics import record_event_loop_lag

    interval = 0.5
    loop = asyncio.get_running_loop()
    expected = loop.time() + interval
    while True:
        await asyncio.sleep(max(0.0, expected - loop.time()))
        now = loop.time()
        record_event_loop_lag(max(0.0, now - expected))
        expected += interval
        if now - expected > interval:  # 严重落后（GC/阻塞），重新对齐
            expected = now + interval


async def _startup() -> None:
    """应用启动逻辑（企业化 P2-1.1：由 lifespan 协议驱动，替代 on_event("startup")）。"""
    logger.info("Starting {} v{}", settings.APP_NAME, settings.APP_VERSION)
    logger.info("Debug mode: {}", settings.DEBUG)
    logger.info("API docs available at http://{}:{}/docs", settings.HOST, settings.PORT)
    # 企业化 P0-4：链路追踪接线——OTel 已转为硬依赖（requirements.txt），
    # 此处初始化 tracer；未配置 OTEL_EXPORTER_OTLP_ENDPOINT 时仅注册 provider
    # （本地 span 构建，无导出开销），配置后自动导出至 OTLP 后端。
    from agentix.observability.tracing import init_tracing

    init_tracing(
        service_name=settings.APP_NAME,
        service_version=settings.APP_VERSION,
    )
    # Phase H · W0：事件循环滞后采样（EVENT_LOOP_LAG_METRIC_ENABLED，默认关）。
    if settings.EVENT_LOOP_LAG_METRIC_ENABLED:
        global _lag_sampler_task
        try:
            _lag_sampler_task = asyncio.get_running_loop().create_task(
                _event_loop_lag_sampler(), name="event-loop-lag-sampler"
            )
            logger.info("event loop lag sampler enabled (interval=0.5s)")
        except RuntimeError:  # pragma: no cover - 无运行事件循环时跳过
            logger.warning("event loop lag sampler skipped (no running loop)")
    # 鉴权模式告警行（不打印密钥值本身）
    logger.info("auth mode: {}", settings.REQUIRE_API_KEY)
    # P0-6：启动期消费 resolved_secret_key —— 显式配置直接使用；
    # dev 缺失时临时生成+强告警；生产缺失直接拒绝启动（方案 P0-6 验收 1）。
    try:
        settings.resolved_secret_key
        secret_key_state = "explicit" if settings.SECRET_KEY else "temporary(dev only)"
    except RuntimeError as exc:
        logger.error("SECRET_KEY check failed: {}", exc)
        raise
    logger.info("secret_key: {}", secret_key_state)
    logger.info("auth enabled: {}", settings.REQUIRE_API_KEY)
    if not settings.REQUIRE_API_KEY:
        logger.warning("auth enabled: False — all endpoints are public; enable before production")
    if not settings.API_KEY and settings.REQUIRE_API_KEY:
        logger.error("REQUIRE_API_KEY=True but API_KEY is empty; auth will reject all requests")

    # P2-4：配置自检——比对 .env 与 .env.example 的**字段集合**。
    # 缺项会让 Pydantic Settings 静默回落到默认值（语义上合法、行为上危险），
    # 故此处在启动日志里显式告警。只比字段不比值（值含密钥，绝不进日志），
    # 且只告警不抛异常（自检不得成为新的故障源）。
    from agentix.config.selfcheck import run_startup_selfcheck

    run_startup_selfcheck()

    # 第四轮修复（2026-09-23）：把检索域的生产配置校验**真正接上**。
    #
    # 此前 `validate_app_settings()` 只被一个测试调用、**启动路径从不调用它**，
    # 于是一整族生产配置问题（限流关闭、输出过滤/PII 脱敏关闭、milvus 未开向量归一化、
    # 超时参数非法……）既不会被拦住、也不会留下任何日志。比"死代码"更糟的是：
    # 调用它的那个测试 docstring 写着"启动校验必须报错"，覆盖率是绿的，
    # 读者会合理推断"这条校验在生效"。
    #
    # 最具体的一个危害：`VECTOR_STORE_BACKEND=milvus` + `VECTOR_NORMALIZE=false` 的
    # **唯一**拦截点就在这个函数里，误配置的后果不是崩溃，而是**静默的相似度口径错误**
    # （milvus_impl 注释自述"归一化时 IP metric ≡ cosine"）——结果错、无异常、无日志。
    _run_settings_validation()

    # Auto-create database tables on startup
    import agentix.infrastructure.persistence.db.models  # noqa: F401 - Import all models to register them with Base
    logger.info("Creating database tables if not exist...")
    Base.metadata.create_all(bind=engine)
    # R3.1: 检索域建表（PG 落 retrieval schema / SQLite 同文件）。此前仅在测试与
    # 迁移脚本中调用、启动流程缺失，全新部署会缺表。
    from agentix.retrieval.infra.db.session import create_db_and_tables

    create_db_and_tables()
    logger.info("Database tables ready")

    # P0-1：初始化 LangGraph checkpointer 并（成功时）用 Redis 持久化图
    # 覆盖模块级默认图。降级路径在 init_checkpointer 内部处理（包未装/
    # 显式 memory 配置/Redis 失败 → MemorySaver），此处仅需兜底防御。
    try:
        from agentix.infrastructure.persistence.checkpointer import init_checkpointer
        from agentix.infrastructure.persistence.checkpointer import (
            get_degraded_reason,
        )
        ckpt = await init_checkpointer()
        from agentix.orchestration.langgraph import lg_builder
        lg_builder.graph = lg_builder.build_graph(checkpointer=ckpt)
        # Phase 19：supervisor 拓扑共享同一 checkpointer（multi_agent flag
        # 开启时 get_active_graph 才会用到；flag-off 不受影响）。
        lg_builder.supervisor_graph = lg_builder.build_supervisor_graph(checkpointer=ckpt)
        logger.info(
            "graph compiled with checkpointer: {}{}",
            type(ckpt).__name__,
            "" if get_degraded_reason() is None else " (degraded)",
        )
    except Exception as exc:  # noqa: BLE001 - lg_builder.graph 仍为模块级默认图
        logger.error("checkpointer wiring failed, using default in-memory graph: {}", exc)

    # Phase 16：HITL 三栈桥接线（**仅 flag 开启时**）。默认
    # HUMAN_REVIEW_ENABLED=false ⇒ 不接线 ⇒ ToolExecutor 保持
    # hook=None 的零变化路径（Strangler 字节等价）。
    if bool(getattr(settings, "HUMAN_REVIEW_ENABLED", False)):
        try:
            from agentix.policies.hitl.hitl_bridge import install_executor_hook

            install_executor_hook()
            logger.info("HITL bridge hook installed (HUMAN_REVIEW_ENABLED=true)")
        except Exception as exc:  # noqa: BLE001 - 接线失败不影响启动（闸门仍 fail-closed）
            logger.error("HITL bridge hook install failed: {}", exc)

    # Phase 21：Skills / Hooks 装配（**仅 flag 开启时**，同 HITL 模式）。
    # 默认 SKILLS_ENABLED=false / HOOKS_ENABLED=false ⇒ 组合根零接线
    # ⇒ executor 观测钩子保持 None、read_skill 不注册（Strangler 零变化）。
    if bool(getattr(settings, "SKILLS_ENABLED", False)):
        try:
            from agentix.agent.skills.install import (
                install_read_skill_tool,
                install_skill_draft_tool,
            )

            install_read_skill_tool()
            install_skill_draft_tool()  # P6-6.3（同 SKILLS_ENABLED gate）
            logger.info("read_skill/create_skill_draft tools installed (SKILLS_ENABLED=true)")
        except Exception as exc:  # noqa: BLE001 - 接线失败不影响启动
            logger.error("skill tool install failed: {}", exc)
    # F1b2·M-P1-4（AGENTS 级联）：read_agents_file 按需取回工具装配。
    # 默认 AGENTS_CASCADE_ENABLED=false ⇒ 不声明不注册（Strangler 零变化）。
    if bool(getattr(settings, "AGENTS_CASCADE_ENABLED", False)):
        try:
            from agentix.agent.context.install import install_read_agents_file_tool

            install_read_agents_file_tool()
            logger.info("read_agents_file tool installed (AGENTS_CASCADE_ENABLED=true)")
        except Exception as exc:  # noqa: BLE001 - 接线失败不影响启动
            logger.error("read_agents_file tool install failed: {}", exc)
    if bool(getattr(settings, "HOOKS_ENABLED", False)):
        try:
            from agentix.agent.hooks.install import install_tool_hooks

            install_tool_hooks()
            logger.info("lifecycle hooks installed (HOOKS_ENABLED=true)")
        except Exception as exc:  # noqa: BLE001 - 接线失败不影响启动
            logger.error("lifecycle hooks install failed: {}", exc)

    # Phase 23：出站通知工具装配（**仅 flag 开启时**，同上模式）。
    # 默认 NOTIFY_USER_ENABLED=false ⇒ notify_user 不注册（Strangler 零变化）。
    if bool(getattr(settings, "NOTIFY_USER_ENABLED", False)):
        try:
            from agentix.application.services.notify_bridge import (
                install_notify_user_tool,
            )

            install_notify_user_tool()
            logger.info("notify_user tool installed (NOTIFY_USER_ENABLED=true)")
        except Exception as exc:  # noqa: BLE001 - 接线失败不影响启动
            logger.error("notify_user tool install failed: {}", exc)

    # P1（任务执行）：任务写工具装配（**仅 flag 开启时**，同上模式）。
    # 默认 TASK_EXEC_ENABLED=false ⇒ 两个写工具不注册（Strangler 零变化）。
    if bool(getattr(settings, "TASK_EXEC_ENABLED", False)):
        try:
            from agentix.application.services.task_tools_bridge import (
                install_task_write_tools,
            )

            install_task_write_tools()
            logger.info("task write tools installed (TASK_EXEC_ENABLED=true)")
        except Exception as exc:  # noqa: BLE001 - 接线失败不影响启动
            logger.error("task write tools install failed: {}", exc)

    # F1b2·M-P1-3（MEMORY 索引制）：MEMORY_INDEX 单开（TASK_EXEC 关）时也要
    # 有 obs:// 指针的取回端 ⇒ 单独装 read_artifact handler（幂等，TASK_EXEC
    # 已装过则为重复同名注册）。默认双 flag 全关 ⇒ 零接线（Strangler 零变化）。
    if bool(getattr(settings, "MEMORY_INDEX_ENABLED", False)):
        try:
            from agentix.application.services.task_tools_bridge import (
                install_artifact_read_tool,
            )

            install_artifact_read_tool()
            logger.info("read_artifact tool installed (MEMORY_INDEX_ENABLED=true)")
        except Exception as exc:  # noqa: BLE001 - 接线失败不影响启动
            logger.error("read_artifact tool install failed: {}", exc)

    # A2b（统一执行链 Stage A）：Tool Gateway 收编装配（**仅 flag 开启时**，
    # 同上模式）。默认 TOOL_GATEWAY_ENFORCED=false ⇒ 五项 KG/检索工具保持
    # 节点直连（Strangler 零变化）；flag-on 后 search/cypher_query/
    # predefined_cypher/microsoft_graphrag_query/text2sql_query 的节点执行缝
    # 改经 ToolExecutor 治理链（handler 由本块安装）。
    if bool(getattr(settings, "TOOL_GATEWAY_ENFORCED", False)):
        try:
            from agentix.application.services.kg_gateway_bridge import (
                install_kg_gateway_handlers,
            )

            install_kg_gateway_handlers()
            logger.info("KG gateway handlers installed (TOOL_GATEWAY_ENFORCED=true)")
        except Exception as exc:  # noqa: BLE001 - 接线失败不影响启动（缝回落直连）
            logger.error("KG gateway handler install failed: {}", exc)

    # A2c（统一执行链 Stage A）：记忆三工具装配（**仅 flag 开启时**，同上模式）。
    # 默认 MEMORY_TOOLS_ENABLED=false ⇒ remember/recall/forget 不声明不注册
    # （Strangler 零变化）；flag-on 后三工具经 ToolExecutor 治理链执行。
    if bool(getattr(settings, "MEMORY_TOOLS_ENABLED", False)):
        try:
            from agentix.application.services.memory_tools_bridge import (
                install_memory_tools,
            )

            install_memory_tools()
            logger.info("memory tools installed (MEMORY_TOOLS_ENABLED=true)")
        except Exception as exc:  # noqa: BLE001 - 接线失败不影响启动
            logger.error("memory tools install failed: {}", exc)

    # P2（延迟工具面）：tool_search 召回工具装配（**仅延迟列表非空时**，
    # 同上模式）。默认 TOOL_DEFERRED_TOOLS="" ⇒ 不声明不注册（Strangler 零变化）。
    if getattr(settings, "TOOL_DEFERRED_TOOLS", ""):
        try:
            from agentix.tools.adapters.tool_search import install_tool_search_tool

            install_tool_search_tool()
            logger.info(
                "tool_search installed (TOOL_DEFERRED_TOOLS={})",
                settings.TOOL_DEFERRED_TOOLS,
            )
        except Exception as exc:  # noqa: BLE001 - 接线失败不影响启动
            logger.error("tool_search install failed: {}", exc)

    # P4（岗位对照·异常闭环 4.3）：计划态工具装配（**仅 flag 开启时**，
    # 同上模式）。默认 AGENT_ROLLBACK_ENABLED=false ⇒ 不声明不注册
    # （Strangler 零变化）；开关打开后 enter/exit_plan_mode 经 ToolExecutor
    # 治理链执行（风险/权限/HITL 闸门全部在前，无绕过路径）。
    if bool(getattr(settings, "AGENT_ROLLBACK_ENABLED", False)):
        try:
            from agentix.application.services.rollback import install_plan_mode_tools

            install_plan_mode_tools()
            logger.info("enter/exit_plan_mode tools installed (AGENT_ROLLBACK_ENABLED=true)")
        except Exception as exc:  # noqa: BLE001 - 接线失败不影响启动
            logger.error("plan mode tools install failed: {}", exc)

    # F1c·A-P1-2（统一执行链 Stage F）：TodoWrite 工具装配（**仅 flag 开启
    # 时**，同上模式）。默认 AGENT_LLM_PLANNER_ENABLED=false ⇒ 不声明不注册
    # （Strangler 零变化）；开关打开后 todo_write 经 ToolExecutor 治理链执行
    # （风险/权限/HITL 闸门全部在前，无绕过路径）。
    if bool(getattr(settings, "AGENT_LLM_PLANNER_ENABLED", False)):
        try:
            from agentix.application.services.todo_write import install_todo_write_tool

            install_todo_write_tool()
            logger.info("todo_write tool installed (AGENT_LLM_PLANNER_ENABLED=true)")
        except Exception as exc:  # noqa: BLE001 - 接线失败不影响启动
            logger.error("todo_write tool install failed: {}", exc)

    # P5（岗位对照·Proactive 5.1/5.2）：cron 定时任务装配（**仅 flag 开启时**，
    # 同上模式）。默认 CRON_ENABLED=false ⇒ 不声明不注册不启动循环（Strangler
    # 零变化）；开关打开后 cron_* 工具经 ToolExecutor 治理链执行（L1 → 审批半），
    # 后台 tick 循环按 CRON_TICK_SECONDS 周期跑 CronScheduler.tick。接线失败
    # 不阻断启动（同 plan mode 模式）。
    if bool(getattr(settings, "CRON_ENABLED", False)):
        try:
            from agentix.application.services.cron_scheduler import (
                CronScheduler,
                install_cron_tools,
                start_cron_scheduler,
            )

            _cron_scheduler = CronScheduler(runner=_build_cron_runner())
            install_cron_tools(_cron_scheduler)
            start_cron_scheduler(_cron_scheduler)
            logger.info(
                "cron scheduler installed (CRON_ENABLED=true, tick={}s)",
                settings.CRON_TICK_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - 接线失败不影响启动
            logger.error("cron scheduler install failed: {}", exc)

    # P2（注册表对账）：三表（声明清单 / spec 注册表 / executor handler）漂移
    # 审计——只告警不阻断启动（诊断只读：不改任何注册状态）。默认配置下审计
    # 结果应为 ok（已知旁路如 search 有显式豁免说明）。
    try:
        from agentix.tools.adapters.integrity import audit_tool_registries

        _audit = audit_tool_registries()
        if not _audit.ok:
            logger.warning("tool registry drift detected: {}", _audit)
    except Exception as exc:  # noqa: BLE001 - 审计是诊断，不阻断启动
        logger.debug("tool registry audit skipped: {}", exc)

    # Crash recovery: resume any interrupted threads
    try:
        from agentix.application.services.breakpoint_resume import get_checkpoint_recovery
        recovery = get_checkpoint_recovery()
        active_threads = await recovery.get_active_threads()
        if active_threads:
            logger.info("Found {} active threads, attempting recovery...", len(active_threads))
            # P0-1 约定（方案 10.4.1）：不得 `from ...lg_builder import graph`——值绑定
            # 会在启动期 Redis 图覆盖前固化旧图（导致扫 active_threads 后恢复用错图）。
            # 这里动态读取 lg_builder.graph 属性，始终拿到当前编译图。
            recovered = []
            for thread_id in active_threads:
                metadata = await recovery.get_checkpoint_metadata(thread_id)
                if metadata and (time.time() - metadata.get("updated_at", 0)) > 60:
                    result = await recovery.resume_thread(thread_id, lg_builder.graph)
                    if result:
                        recovered.append(thread_id)
            logger.info("Recovered {} threads: {}", len(recovered), recovered)
    except Exception as exc:
        logger.warning("Crash recovery check failed: {}", exc)


async def _shutdown() -> None:
    """应用退出逻辑（企业化 P2-1.1：由 lifespan 协议驱动）。"""
    logger.info("Shutting down {}", settings.APP_NAME)

    # P5：先取消 cron 后台 tick 循环（不再起新任务；执行中任务被取消 ⇒
    # _execute 先 release 簿记再透传 CancelledError——重启后按计划恢复）。
    try:
        from agentix.application.services.cron_scheduler import stop_cron_scheduler

        stop_cron_scheduler()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("cron scheduler stop failed: {}", exc)

    # Phase H · W0：取消事件循环滞后采样任务。
    global _lag_sampler_task
    if _lag_sampler_task is not None:
        _lag_sampler_task.cancel()
        _lag_sampler_task = None

    # Phase H · W3：关闭共享 httpx 出口连接池（未构建时零开销）。
    try:
        from agentix.infrastructure.http import aclose_shared_async_client

        await aclose_shared_async_client()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("shared http client close failed: {}", exc)

    # Phase H · W4：轨迹异步写兜底 flush（flag 关 = no-op）。
    try:
        from agentix.application.services.trajectory import flush_trajectory_writes

        await flush_trajectory_writes()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("trajectory flush on shutdown failed: {}", exc)

    # Phase H · W4：事件批写尾巴 flush（redis_stream 后端有 aclose = 取消
    # 定时器并提交 pending；memory/jsonl 无此方法零开销）。
    try:
        from agentix.agent.events.port import get_event_port

        port = get_event_port()
        aclose = getattr(port, "aclose", None)
        if callable(aclose):
            await aclose()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("event port flush on shutdown failed: {}", exc)

    # 企业化 P0-4：flush/关闭追踪导出器
    from agentix.observability.tracing import shutdown_tracing

    shutdown_tracing()

    # Cleanup LightRAG resources
    try:
        lightrag_service = get_lightrag_service()
        await lightrag_service.cleanup()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Failed to cleanup LightRAG service: {exc}")


@application.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Phase H · W3：准入拒绝 → 429（策略性过载，非服务器故障）。
    # 放在通用 500 之前判定；AdmissionDenied 惰性 import 避免加载期耦合。
    try:
        from agentix.runtime.control_plane import AdmissionDenied

        if isinstance(exc, AdmissionDenied):
            logger.warning(
                "admission denied on {} {}: {}", request.method, request.url, exc
            )
            return JSONResponse(
                status_code=429,
                content={
                    "error": "Too Many Requests",
                    "message": "Server busy: admission control rejected the run",
                    "detail": str(exc),
                },
            )
    except ImportError:  # pragma: no cover - control_plane 恒存在
        pass
    logger.error("Unhandled exception on {} {}: {}", request.method, request.url, exc)
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal Server Error",
            "message": "An unexpected error occurred",
        },
    )


# 遗留修复：AppError → 标准错误响应的全局转换器。
# 此前检索域路由抛出的 AppError（DOCUMENT_NOT_FOUND / UNAUTHORIZED 等）没有
# 任何转换器，Starlette 一律收敛为 500——authz.py 精心设置的 status_code
# 形同虚设，前端也无法区分"资源不存在"与"服务端错误"。
# 语义：错误码在 ERROR_CODE_TO_STATUS 登记过 → 用映射状态码；未登记 →
# 尊重 AppError.status_code（dataclass 默认 400）。响应体与检索域
# response.error() 的标准结构完全一致（request_id/error_code/message/user_hint）。
@application.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    status = ERROR_CODE_TO_STATUS.get(exc.error_code)
    if status is None:
        status = exc.status_code
    body = retrieval_error_body(
        error_code=exc.error_code,
        message=exc.message,
        user_hint=exc.user_hint,
        request_id=getattr(request.state, "request_id", ""),
        details=exc.details,
    )
    logger.warning(
        "AppError on {} {}: {} ({})",
        request.method,
        request.url.path,
        exc.error_code,
        status,
    )
    return JSONResponse(status_code=status, content=body)


if __name__ == "__main__":
    uvicorn.run(
        "agentix.main:application",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level="info",
    )

# Backwards-compatible alias for tooling expecting `app` symbol
app = application
