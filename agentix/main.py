"""
My Agentix FastAPI application entry point.
"""

import asyncio
import contextlib
import os
import re
import secrets
import signal
import threading
import time
import uvicorn
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, List, Optional
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from agentix.application.services.lightrag_service import get_lightrag_service

# M3-2（§7.5 第②步）：装配期注册节点 handler。本文件是 FastAPI 装配根，建图路径
# （_startup → lg_builder / supervisor 惰性 getter）在运行期才发生，模块级这里是
# 唯一「先注册、后建图」的挂线位；编排层不得反向 import 本装配文件。
import agentix.application.wiring  # noqa: F401 - 装配期注册节点 handler（§7.5 第②步）
from agentix.config import settings
from agentix.infrastructure.core import configure_logging
from agentix.infrastructure.core.database import Base, get_engine
from agentix.interfaces.http import lightrag_router
from agentix.interfaces.http.public_paths import PUBLIC_PATHS, PUBLIC_PREFIXES
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
    Exempts probes, docs and the Prometheus scrape endpoint — the list lives in
    ``agentix/interfaces/http/public_paths.py`` and is shared with the rate-limit
    middleware. The root ``/api`` info endpoint is deliberately NOT exempt: it is
    not a probe (``/health`` is), so there is no reason to disclose the version
    anonymously when auth is on.
    """

    EXEMPT_PATHS = PUBLIC_PATHS

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
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
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
# 日志 shed load）。按客户端 IP 令牌桶限流，覆盖测试数据编排域 + 检索域全部入口；
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
    # 下面三处只回异常类名、不回 str(e)：/health 是公开无鉴权端点，而这些异常文本里
    # 常带连接串（Redis DSN 的密码位就落在其中）。别按"日志吞原因"的口径把它"修宽"。
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


#: 出厂默认关闭、但决定「这台实例能不能直接对外」的闸门。
#: 判据是「关着会让实例不该对外」，不是「凡是新 flag 都列进来」——
#: `TOOL_HANDLER_CONTRACT_ENABLED`（§24.3 T1 ⑧）只产启动日志、不改变任何执行语义，
#: 关着不构成对外风险，列进来只会让这条告警变成噪音。
_INSECURE_GATE_FLAGS: tuple[str, ...] = (
    "REQUIRE_API_KEY",
    "V1_REQUIRE_JWT",
    "RATE_LIMIT_ENABLED",
    "HUMAN_REVIEW_ENABLED",
    "TOOL_GUARDRAILS_ENABLED",
    "TOOL_SYNC_OFFLOAD_ENABLED",
    "MCP_SUBPROCESS_ENV_SCRUB",
    # §24.3 T1 ⑪：关着时任意登录用户可改写任意 kb 的 base_url（把该租户的提问外送）
    "SETTINGS_AUTHZ_ENABLED",
)


def _warn_insecure_posture() -> None:
    """G6：把「当前安全姿态」变成启动时一条响亮日志，而不是靠人记住该开哪些 flag。

    只在**未显式声明** APP_ENV 时告警——显式声明即代表运维已经做过判断，
    再叫就是噪音。生产/staging 身份下这条件不成立（STRICT_VALIDATION 分支另有 raise）。
    """
    if (settings.APP_ENV or "").strip():
        return
    off = [name for name in _INSECURE_GATE_FLAGS if not getattr(settings, name, False)]
    if not off:
        return
    from agentix.retrieval.settings import settings as _rs

    logger.warning(
        "SECURITY POSTURE: APP_ENV is not declared, so env is inferred from DEBUG={} "
        "(=> env={!r}). {} gate flag(s) are OFF and this instance must NOT be exposed "
        "to untrusted traffic: {}. Set APP_ENV=production (and enable the gates) before "
        "any real deployment.",
        settings.DEBUG,
        _rs.ENVIRONMENT,
        len(off),
        ", ".join(off),
    )


def _run_settings_validation() -> None:
    """执行检索域的生产配置校验，把结果变成**启动日志**，生产/staging 则**拒绝启动**。

    语义（三态，与"自检不得成为新的故障源"的原则一致）：

    ==================  ==========================  ================================
    校验结果            开发环境                    硬失败面（STRICT_VALIDATION）
    ==================  ==========================  ================================
    无 error            记一条 info（含告警条数）    同左
    有 warning          逐条 warning（不阻断）       同左
    有 error            逐条 error（**不阻断**）     逐条 error + **raise**
    ==================  ==========================  ================================

    硬失败面 = production **+ staging**（口径同 `resolved_secret_key`）。staging 的价值是
    「用生产的失败语义提前跑一遍」，只判 PRODUCTION 会让显式声明 `APP_ENV=staging` 的
    部署**静默丢掉**这道 raise——G6 之前它靠 `DEBUG=false` 反推出 "production" 才拿到。

    为什么生产环境要拒绝启动：这些 error 对应的都是"能被静默容忍、但会导致错误行为"的
    配置（典型：milvus + 未归一化 ⇒ 相似度口径错，但接口仍返回 200 和一堆结果）。
    **让它带着错误语义对外服务，比让它启动时就响亮地失败要危险得多。**

    为什么开发环境不阻断：本地跑测试/调试时常有意关掉限流或输出过滤，
    此时阻断会把"校验"变成新的故障源——但也必须留下日志，否则又回到"静默"。
    """
    from agentix.retrieval.settings import settings as _rs
    from agentix.retrieval.settings import validate_app_settings

    _warn_insecure_posture()
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
    if _rs.STRICT_VALIDATION:
        raise RuntimeError(
            "refusing to start: {} settings validation error(s) in {} "
            "(env={}): {}".format(
                len(errors),
                "production" if _rs.PRODUCTION else "staging",
                _rs.ENVIRONMENT,
                "; ".join(errors),
            )
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


# E4：embedding 缓存启动预热任务句柄。句柄必须存模块级——asyncio 只持弱引用，
# 不存取的 create_task 任务可能被 GC 掉，表现为"预热偶发没跑"这种最难查的形态。
# flag（EMBEDDING_CACHE_PREWARM_ENABLED）默认关 ⇒ 默认路径不创建任务、不发网络请求。
_embedding_prewarm_task: asyncio.Task | None = None


async def _embedding_prewarm_worker() -> None:
    """后台首刷 embedding 缓存（一次性，不阻塞启动）。

    两个刻意的实现选择：① 预热本体是**同步**的（sqlite + 同步 http 客户端），必须经
    ``asyncio.to_thread`` 下放，否则冷启动那段批量请求会把事件循环按住；
    ② ``run_prewarm_from_settings`` 自己已经 fail-open，这里再兜一层是因为**导入它**这件事
    也可能炸（依赖缺失、循环导入），而启动路径没有任何理由因为预热失败而中断（纪律 2）。
    """
    try:
        from agentix.retrieval.infra.embedding.prewarm import run_prewarm_from_settings

        report = await asyncio.to_thread(run_prewarm_from_settings)
        if report.get("skipped"):
            logger.info("embedding cache prewarm skipped: reason={}", report["skipped"])
        elif report.get("error"):
            logger.warning("embedding cache prewarm degraded: {}", report)
        else:
            logger.info(
                "embedding cache prewarm scheduled result: requested={} cached={} embedded={} elapsed_ms={}",
                report.get("requested"),
                report.get("cached"),
                report.get("embedded"),
                report.get("elapsed_ms"),
            )
    except Exception as exc:  # noqa: BLE001 - 预热是加速件：失败只留痕，不影响服务可用性（纪律 2）
        logger.warning("embedding cache prewarm task failed: {}", exc)


# T19（审批恢复规划 · Q9 已裁决：到期自动拒绝）：审批时限扫描任务句柄。
# 与上面两条同因：asyncio 只持弱引用，句柄必须存模块级，否则"偶发没扫"最难查。
# `HITL_APPROVAL_DEADLINE_S <= 0`（默认）= 一个任务都不创建（纪律 1）。
_hitl_deadline_task: Optional[asyncio.Task] = None
# T19 第 2 轮返工：起任务时用的那台桥（持有扫描租约）。关停要靠它**让出**租约——
# 进程走了还攥着租约，另一个 worker 得等到 TTL 到期才接手，这段时间没人扫。
# 刻意存句柄而不是在关停时调 `get_hitl_bridge()`：那会在关停路径上凭空装配一个
# 带库面/指标面的单例，比"少释放一次租约"严重得多。
_hitl_deadline_bridge: Any = None

# T22：checkpointer 降级后的升级重试任务句柄。同因必须存模块级——asyncio 只持弱引用，
# 不存句柄的任务可能被 GC 掉，表现成「重试偶发没跑」。
# `CHECKPOINTER_RETRY_INTERVAL_S <= 0`（默认 0 = 关）= 一个任务都不创建（纪律 1）。
_checkpointer_upgrade_task: Optional[asyncio.Task] = None


def _hitl_sweep_interval(deadline_s: float) -> float:
    """扫描周期（纯函数，便于测试三个边界）：deadline 的 1/4，夹在 [1, 30] 秒。

    为什么是 1/4：扫描是**周期**而非定时器，一条 pending 最坏会多存活 1/4 个
    deadline 才被扫到——这个额外存活的上界与 deadline 同量级，可解释。
    为什么夹上界 30 秒：deadline 配到小时级时按 1/4 扫变成几十分钟才读一次库，
    归因延迟会大到运维以为"没生效"；夹下界 1 秒：再密就是拿库空转。
    """
    return min(max(float(deadline_s) / 4.0, 1.0), 30.0)


async def _hitl_deadline_sweeper(bridge: Any, deadline_s: float, interval_s: float) -> None:
    """周期扫描到期 pending 并自动拒绝（fire-and-forget，不阻塞启动/关停）。

    ``expire_overdue`` 自己已 fail-open（读库失败返回空、单行失败跳过），这里再兜
    一层宽 except 是因为**取消之外的一切异常都不该让"没人再扫"**：扫描任务死了
    就等于超时语义静默失效，那比多打一条 warning 严重得多（纪律 2）。
    """
    while True:
        try:
            await bridge.expire_overdue(deadline_s)
        except Exception as exc:  # noqa: BLE001 - 扫描失败不拖垮主链：留痕后下一轮再试
            logger.warning(
                "HITL approval deadline sweep failed (retrying next tick): {}", exc
            )
        await asyncio.sleep(interval_s)


def _start_hitl_deadline_sweeper(bridge: Any) -> bool:
    """起审批时限扫描任务；返回是否真的起了任务（``False`` = 无任务可取消）。

    **四条按序判定都在这里、且只在这里**（纪律 6 ④：同一个判定不许两个源）：
    ① ``HITL_APPROVAL_DEADLINE_S <= 0``（默认）⇒ 直接返回，**一个任务都不创建、
       一行日志都不打**（纪律 1 的"默认路径不多一个任务"）；
    ② deadline>0 但 ``HUMAN_REVIEW_ENABLED`` 为假 ⇒ **没有桥、也没有 pending**
       （接线只在 ``install_executor_hook`` 里发生），扫描无从谈起，**显式告警
       一次**后返回；
    ③ deadline>0 但 ``HITL_PENDING_PERSIST_ENABLED`` 为假 ⇒ 没有库可扫（内存里
       那几条 pending 只能由本实例裁决，超时语义落不到真源），**显式告警一次**后
       返回——"配了却没生效"是最难查的形态，不静默当"已经在管"；
    ④ ``bridge is None``（hook 安装失败时调用方只会传进 None）⇒ 告警一次后返回，
       防"扫一个 None"。

    四道判定全部通过 ⇒ ``create_task`` 后台跑，绝不 ``await``（启动不得被扫描阻塞），
    并把**这台桥**登记进模块级 ``_hitl_deadline_bridge``：关停要靠它让出扫描租约
    （见 :func:`_shutdown`），本函数因此是「任务句柄 + 桥句柄」的唯一写点。

    ``HITL_PENDING_PERSIST_ENABLED`` 在 ``_resolve_pending_store`` 处**另有**一道
    判据——那里判的是「装不装库面」（要不要读写这张表），这里判的是「起不起扫描」
    （有没有东西可周期性扫）；两件事，不是同一个判定的两个源。

    deadline 取值走 :func:`resolve_approval_deadline_s`（与 :func:`get_hitl_bridge`
    同一个函数、同一个值，不在本函数里另拼一份字符串常量）。
    """
    global _hitl_deadline_task, _hitl_deadline_bridge
    # 本函数是这两条句柄的唯一写点：进入时先归零，只有"真的起了任务"才把桥挂上，
    # 保证任何一条提前返回的路径都不会留下一个"以为持有租约"的句柄。
    _hitl_deadline_bridge = None

    # 惰性 import：让 main 顶部不无条件拉起桥模块（它连带 HITL 配置面与闸门面）。
    # 诚实说明：本函数启动时**必走到**这一行，所以这里省下的是"main 顶部多一个
    # 模块级依赖"，不是"flag-off 时零 import"。纪律 1 的可观测面是
    # 「deadline=0 ⇒ 无任务、不读库、不连库」——这一行只完成一次 import，
    # 不建任务、不碰库。
    from agentix.policies.hitl.hitl_bridge import resolve_approval_deadline_s

    deadline_s = resolve_approval_deadline_s()
    if deadline_s <= 0:
        return False
    if not bool(getattr(settings, "HUMAN_REVIEW_ENABLED", False)):
        logger.warning(
            "HITL_APPROVAL_DEADLINE_S={} is ignored: HUMAN_REVIEW_ENABLED is false, "
            "so no HITL bridge/pending exists and approval expiry is NOT enforced",
            deadline_s,
        )
        return False
    if not bool(getattr(settings, "HITL_PENDING_PERSIST_ENABLED", False)):
        logger.warning(
            "HITL_APPROVAL_DEADLINE_S={} is ignored: HITL_PENDING_PERSIST_ENABLED is false, "
            "so there is no pending store to sweep and approval expiry is NOT enforced",
            deadline_s,
        )
        return False
    if bridge is None:
        logger.warning(
            "HITL approval deadline sweeper skipped: HITL bridge unavailable "
            "(hook install failed)"
        )
        return False
    interval_s = _hitl_sweep_interval(deadline_s)
    try:
        _hitl_deadline_task = asyncio.get_running_loop().create_task(
            _hitl_deadline_sweeper(bridge, deadline_s, interval_s),
            name="hitl-approval-deadline-sweeper",
        )
    except RuntimeError:  # pragma: no cover - 无运行事件循环时跳过
        logger.warning("HITL approval deadline sweeper skipped (no running loop)")
        return False
    # 任务真起来了才登记桥：关停要经它让出扫描租约（见 `_hitl_deadline_bridge`）。
    _hitl_deadline_bridge = bridge
    logger.info(
        "HITL approval deadline sweeper started (deadline={}s, interval={}s)",
        deadline_s,
        interval_s,
    )
    return True


def _wire_graph_checkpointer(ckpt: Any) -> None:
    """把 checkpointer 装进模块级图并留痕——启动期与升级重试的**唯一**装配点。

    抽成函数的理由（纪律 6 ④）：T22 的升级重试要把新的 AsyncRedisSaver 换进正在服务的
    图；若重试循环另写一份 ``build_graph`` / ``build_supervisor_graph`` 调用，「图当前
    挂哪个 saver」就出现了两个源，改一处漏一处。顺带让 Phase 19 的「supervisor 拓扑
    共用同一 checkpointer」这条约束也不必在第二处重复声明。

    **两张图都编好才一起换（原子）**：先赋值主图、再编 supervisor 的话，后者抛错会留下
    「主图已换新 saver、supervisor 仍挂旧 saver」的半装配态；而升级重试把本函数成功当
    成「升级成功」的判定，半装配态会被当成已恢复。

    失败原样抛出：启动段兜底退回模块级默认图、重试循环按「本轮尝试失败」处理（此时两张
    图都还是旧的，不会留下半装配态）——两种处置不同，判定留在调用点。
    """
    from agentix.infrastructure.persistence.checkpointer import get_degraded_reason
    from agentix.orchestration.langgraph import lg_builder

    graph = lg_builder.build_graph(checkpointer=ckpt)
    # Phase 19：supervisor 拓扑共享同一 checkpointer（multi_agent flag
    # 开启时 get_active_graph 才会用到；flag-off 不受影响）。
    supervisor_graph = lg_builder.build_supervisor_graph(checkpointer=ckpt)
    # 两张都编好之后才落赋值：这里是「要么都换、要么都不换」的唯一分界。
    lg_builder.graph = graph
    lg_builder.supervisor_graph = supervisor_graph
    logger.info(
        # 升级重试时本函数在提交新 saver **之前**调用（装配是成功判定的一环），
        # 此刻 `_degraded_reason` 还是旧的降级原因，因此这里如实打印「装配时刻」的状态。
        "graph compiled with checkpointer: {}{}",
        type(ckpt).__name__,
        "" if get_degraded_reason() is None else " (degraded)",
    )


async def _checkpointer_upgrade_loop(interval_s: float, max_attempts: int) -> None:
    """按**定间隔**重试「MemorySaver → AsyncRedisSaver」升级；成功一次即退出。

    「要不要重试」只读 ``checkpointer.is_upgrade_retryable()``——判定在
    `checkpointer.py` 的降级分支里生成（唯一源），本循环只负责**节奏**与**次数**
    （纪律 6 ④：不在 main 里按配置键或降级原因字符串重判一遍）。

    节奏刻意是定间隔而非指数退避：重试目标是同进程的一个 Redis 连接，退避换不来
    成功率，只让运维多一套要记的口径——间隔就是 `CHECKPOINTER_RETRY_INTERVAL_S`。

    **图装配是升级成功判定的一环**：把唯一装配点 ``_wire_graph_checkpointer`` 作为
    「装配回调」交给 `retry_upgrade_checkpointer()`，由它**在提交新 saver 之前**调用。
    装配失败即本轮尝试失败（saver / 原因 / 指标全留在 checkpointer 模块内处置），
    循环继续下一轮；本函数不另写 `_saver` / `_degraded_reason`（那会立第二个源）。

    失败姿态 fail-open（纪律 2）：任何一次尝试异常都只留痕并继续下一轮；次数用尽打
    一条**明确**的 warning（含已尝试次数与最后一次失败原因）并复位标记，避免任何后续
    调用再判一次「还能升级」而重复刷同一条日志。
    """
    from agentix.infrastructure.persistence.checkpointer import (
        disarm_upgrade_retry,
        get_degraded_reason,
        is_upgrade_retryable,
        retry_upgrade_checkpointer,
    )

    attempts = 0
    while is_upgrade_retryable() and attempts < max_attempts:
        await asyncio.sleep(interval_s)  # 取消点：关停时 CancelledError 从这里抛出
        attempts += 1
        try:
            # 装配回调直接传唯一装配点本身（模块全局查找，不另包一层）：循环因此不持有
            # 第二份「图怎么编」的知识，也不能自己提交 saver。
            upgraded = await retry_upgrade_checkpointer(_wire_graph_checkpointer)
        except Exception as exc:  # noqa: BLE001 - 重试是辅助件：留痕后继续下一轮，主链不受影响（纪律 2）
            logger.warning(
                "checkpointer upgrade attempt {}/{} raised (retrying next tick): {}",
                attempts,
                max_attempts,
                exc,
            )
            continue
        if not upgraded:
            continue
        # 升级成功（含图已装配）：没有别的收尾动作，退出即「成功一次即终止」。
        return

    if is_upgrade_retryable():
        logger.warning(
            "checkpointer upgrade retries exhausted after {} attempt(s) "
            "(interval={}s): session state stays in MemorySaver and will NOT persist. "
            "Last failure: {}",
            attempts,
            interval_s,
            get_degraded_reason(),
        )
        # 复位标记：这一进程不会再有下一次尝试，别让后续调用重复打同一条告警。
        disarm_upgrade_retry()


def _start_checkpointer_upgrade() -> bool:
    """起 checkpointer 升级重试任务；返回是否真的起了任务（``False`` = 无任务可取消）。

    **三道判定按序都在这里、且只在这里**（纪律 6 ④）：
    ① ``CHECKPOINTER_RETRY_INTERVAL_S <= 0``（默认 0 = 关）⇒ 直接返回，**不建任务、
       不打日志、不 import checkpointer 模块**（纪律 1 的"默认路径不多一个任务"）；
    ② ``checkpointer.is_upgrade_retryable()`` 为假（显式 memory / 包未安装 / 已升级 /
       fail-closed）⇒ 返回——「能不能重试」的判据在 checkpointer.py 生成，这里不另判；
    ③ ``CHECKPOINTER_RETRY_MAX_ATTEMPTS <= 0`` ⇒ 显式告警一次后返回：配了节奏却给
       零次尝试等于配了个空壳，不能静默当"已经在重试"。

    三道全过 ⇒ ``create_task`` 后台跑（绝不 ``await``，启动不得被重试阻塞），并把句柄
    写进模块级 ``_checkpointer_upgrade_task``：关停要靠它取消（见 :func:`_shutdown`），
    本函数因此是该句柄的唯一写点。
    """
    global _checkpointer_upgrade_task

    interval_s = float(getattr(settings, "CHECKPOINTER_RETRY_INTERVAL_S", 0.0) or 0.0)
    if interval_s <= 0:
        return False

    from agentix.infrastructure.persistence.checkpointer import is_upgrade_retryable

    if not is_upgrade_retryable():
        return False
    max_attempts = int(getattr(settings, "CHECKPOINTER_RETRY_MAX_ATTEMPTS", 0) or 0)
    if max_attempts <= 0:
        logger.warning(
            "checkpointer upgrade retry skipped: CHECKPOINTER_RETRY_MAX_ATTEMPTS={} "
            "leaves no attempt for CHECKPOINTER_RETRY_INTERVAL_S={}s "
            "(keep the cap >= 1 or set the interval back to 0)",
            max_attempts,
            interval_s,
        )
        return False
    try:
        _checkpointer_upgrade_task = asyncio.get_running_loop().create_task(
            _checkpointer_upgrade_loop(interval_s, max_attempts),
            name="checkpointer-upgrade-retry",
        )
    except RuntimeError:  # pragma: no cover - 无运行事件循环时跳过
        logger.warning("checkpointer upgrade retry skipped (no running loop)")
        return False
    logger.info(
        "checkpointer upgrade retry started (interval={}s, max_attempts={})",
        interval_s,
        max_attempts,
    )
    return True


# T26：任务队列消费 worker 的任务句柄与实例句柄。
# 句柄必须存模块级（asyncio 只持弱引用，不存句柄的任务可能被 GC 掉，表现成
# 「消费偶发不跑」）；实例句柄是关停时调 `stop_worker()` 要用的一方——**不**在关停
# 路径上重新 `get_task_queue()`：那会在关停时凭空装配一个带 Redis 客户端的单例。
_task_queue_worker_task: Optional[asyncio.Task] = None
_task_queue_worker: Any = None


def _on_task_queue_worker_done(task: asyncio.Task) -> None:
    """队列 worker 的结束观察回调（台账表 E G5：异步任务内异常必须可见）。

    「任务创建成功」只证明同步侧走通；真正在跑的是 ``start_worker`` 协程，它后续
    炸掉时若无本回调，异常没有任何消费者——只剩 asyncio 的 "Task exception was
    never retrieved" 栈，不算可观测。取回异常这件事本身也消掉那条提示。
    被取消（``_shutdown`` 走的正是取消路径）与正常结束都不记 ERROR：前者是预期
    关停、后者不是失败。回调自身 fail-open（纪律 2）：观测设施的失败绝不外抛，
    免得搅乱事件循环上正在跑的其它任务。
    """
    try:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        logger.error(
            "task queue worker crashed: err_type={} err={}",
            type(exc).__name__,
            exc,
        )
    except Exception as cb_exc:  # noqa: BLE001 - 回调自身失败只降级为一条 WARNING 留痕，不外抛（纪律 2）
        logger.warning(
            "task queue worker done-callback failed: err_type={} err={}",
            type(cb_exc).__name__,
            cb_exc,
        )


def _start_task_queue_worker() -> bool:
    """起 Redis Stream 队列消费 worker；返回是否真的起了任务。

    ``False`` 的含义：未留下可取消的任务句柄（赋值前失败或 flag-off）；赋值后失败同样
    返回 ``False``，但句柄已落位、关停照常取消（详见下方台账表 E G5 的注释分段说明）。

    **「要不要跑」的判定只有这里一处**（纪律 6 ④）：``TASK_QUEUE_WORKER_ENABLED``
    为假（默认）⇒ 直接返回，**不建任务、不建连接、不打一行日志、不 import 队列模块**
    （纪律 1：flag-off 与基线逐位等价）。``TaskQueue.start_worker`` 自己不读 flag。

    **构造/启动失败 fail-open**（纪律 2）：队列消费是后台件，起不来只 ERROR 留痕，
    绝不阻断服务启动——Web 面（chat / 检索 / 审批）不依赖它。日志里带异常类型，
    避免「只看到一句失败、不知道是 import 坏了还是没事件循环」。

    认领参数不在这里：``_CLAIM_IDLE_MS`` / ``_CLAIM_INTERVAL_S`` /
    ``_CLAIM_MAX_PER_ROUND`` 是 ``task_queue.py`` 的模块常量，``main`` 不重判一遍。
    """
    global _task_queue_worker_task, _task_queue_worker
    # 本函数是两个句柄的唯一写点：进入即归零，任何提前返回都不留"以为在跑"的句柄。
    _task_queue_worker_task = None
    _task_queue_worker = None

    if not bool(getattr(settings, "TASK_QUEUE_WORKER_ENABLED", False)):
        return False

    try:
        # 惰性 import：flag-off 时连队列模块都不加载。
        # 先 import 业务 handler 模块（它在模块级 `queue = get_task_queue()` 上挂
        # `@queue.register(...)`）：不先注册就起消费，每条消息都只会走
        # 「No handler for task type」分支——任务被 ack 成 FAILED，比不启动更误导。
        # 这一行只在 flag-on 路径上执行，flag-off 零副作用（纪律 1）。
        import agentix.application.services.task_handlers  # noqa: F401 - 注册 TaskQueue 业务 handler
        from agentix.application.services.task_queue import get_task_queue

        queue = get_task_queue()
        task = asyncio.get_running_loop().create_task(
            queue.start_worker(), name="task-queue-worker"
        )
        task.add_done_callback(_on_task_queue_worker_done)
        # 台账表 E G5：本保护块覆盖 add_done_callback、句柄赋值、启动日志三步，任一步
        # 失败都走同一条 fail-open 出口返回 False。但"返回 False 之后关停看得见什么"分两段：
        # 句柄赋值先于启动日志 ⇒ 日志自身抛异常时句柄已落位、任务在跑，关停路径仍能取消它；
        # 赋值之前失败 ⇒ 句柄仍是本函数进入即归零的 None，关停无任务可取消。诚实登记：
        # except 侧文案里的"disabled"只描述启动这一步的结果，赋值后失败的窗口内消费面其实
        # 仍在跑，那句日志不区分这两种形态（改它会牵动 test_p171_queue_claim 的文案断言）。
        _task_queue_worker = queue
        _task_queue_worker_task = task
        logger.info(
            "task queue worker started (stream={} group={} consumer={})",
            queue.stream_name,
            queue.consumer_group,
            queue.consumer_name,
        )
    except Exception as exc:  # noqa: BLE001 - 后台消费起不来不阻断启动：ERROR 留痕后降级为"不消费"（纪律 2）
        logger.error(
            "task queue worker start failed (queue consumption disabled): err_type={} err={}",
            type(exc).__name__,
            exc,
        )
        return False
    return True


#: 启动恢复的总墙钟预算（秒）。恢复是「尽力而为」：没恢复完的线程仍是未完成状态，
#: 下一次启动或 /ops/threads/resume 还能接手；但它不能无限拖住服务可用性。
RECOVERY_TOTAL_BUDGET_S = 30.0
#: 同时恢复的线程数上限：串行 = 启动耗时随待恢复线程数线性增长（LG-2-7 的起点）；
#: 无上限 = 启动瞬间打满 LLM / Redis 上游。2 = 两条流水 + 不打爆上游。
RECOVERY_MAX_CONCURRENCY = 2


async def _recover_one_thread(
    recovery: Any, thread_id: str, semaphore: asyncio.Semaphore
) -> Optional[str]:
    """恢复单个线程；成功返回 thread_id，跳过 / 超时 / 失败返回 None。

    单个线程的墙钟上限取 ``AGENT_TIMEOUT``（与该线程在请求路径上的执行上限同源，
    不另立一个超时口径）：卡死的线程只吃掉自己的槽位与这段时间，其余线程照跑。
    """
    from agentix.application.services.breakpoint_resume import ThreadInterruptedError
    from agentix.orchestration.langgraph import lg_builder

    async with semaphore:
        try:
            result = await asyncio.wait_for(
                recovery.resume_thread(thread_id, lg_builder.graph, kind="startup"),
                timeout=settings.AGENT_TIMEOUT,
            )
        except ThreadInterruptedError as exc:
            # LG-2-2：等人工裁决的线程不硬续（会 200 但又挂回去）；留在
            # active 清单里由 ops 面处理，启动流程继续走下一个。
            logger.info(
                "Skipped interrupted thread {} ({} pending): use /ops/hitl/in-loop",
                thread_id,
                exc.pending,
            )
            return None
        except asyncio.TimeoutError:
            # 取消掉的那次续跑不写终态：checkpoint 仍停在未完成步，下一次启动
            # 会再次列到它（恢复可重试，副作用由任务层幂等兜底）。
            logger.warning(
                "Recovery of thread {} exceeded AGENT_TIMEOUT ({}s); left unfinished",
                thread_id,
                settings.AGENT_TIMEOUT,
            )
            return None
    return thread_id if result else None


async def _recover_threads(recovery: Any, thread_ids: List[str]) -> List[str]:
    """按 ``RECOVERY_MAX_CONCURRENCY`` 并发恢复，返回成功恢复的线程 id。"""
    semaphore = asyncio.Semaphore(RECOVERY_MAX_CONCURRENCY)
    results = await asyncio.gather(
        *(_recover_one_thread(recovery, thread_id, semaphore) for thread_id in thread_ids)
    )
    return [thread_id for thread_id in results if thread_id]


async def _startup_crash_recovery() -> None:
    """启动期崩溃恢复：扫描 + 有界并发恢复，整体受 ``RECOVERY_TOTAL_BUDGET_S`` 约束。

    预算耗尽时 ``wait_for`` 取消仍在跑的续跑协程：它们停在未完成步、不写终态，
    下一次启动或 ops 面还能接手。恢复失败一律只留痕，不阻断启动（纪律 2）。
    """
    from agentix.application.services.breakpoint_resume import get_checkpoint_recovery
    from agentix.orchestration.langgraph import lg_builder

    recovery = get_checkpoint_recovery()

    async def _run() -> None:
        # LG-2-1：线程清单来自 checkpointer.alist() 枚举（不再读 Redis 集合），
        # 「未完成」由图状态快照判定；60 秒空闲闸的口径也从 Redis 元数据
        # （updated_at，无生产写入点）换成 checkpoint 自带的 created_at。
        active_threads = await recovery.get_active_threads(
            lg_builder.graph, min_idle_seconds=60
        )
        if not active_threads:
            return
        logger.info("Found {} active threads, attempting recovery...", len(active_threads))
        # P0-1 约定（方案 10.4.1）：不得 `from ...lg_builder import graph`——值绑定
        # 会在启动期 Redis 图覆盖前固化旧图。这里动态读取 lg_builder.graph 属性。
        recovered = await _recover_threads(recovery, active_threads)
        logger.info("Recovered {} threads: {}", len(recovered), recovered)

    try:
        await asyncio.wait_for(_run(), timeout=RECOVERY_TOTAL_BUDGET_S)
    except asyncio.TimeoutError:
        logger.warning(
            "Crash recovery exceeded total budget ({}s); unfinished threads stay "
            "for next start / /ops/threads/resume",
            RECOVERY_TOTAL_BUDGET_S,
        )
    except Exception as exc:
        logger.warning("Crash recovery check failed: {}", exc)


# ---------------------------------------------------------------------------
# LG-3-6：优雅关闭（SIGTERM/SIGINT → 原生 ``RunControl`` drain）
# ---------------------------------------------------------------------------
#: 已安装的信号处理器（signum → 原处理器）。``signal.signal`` 是**进程级**状态，
#: 安装/卸载必须成对，否则用例之间会互相污染，且 uvicorn 的退出路径会被永久替换。
_drain_prev_handlers: dict = {}
#: drain 上限计时器（daemon 线程）。到点仍在跑 ⇒ 强制退出，不无限等（验收第 ③ 条）。
_drain_deadline_timer: Optional[threading.Timer] = None


def _arm_drain_deadline(timeout: float) -> None:
    """起一条上限计时线程：到点仍未退出就强制退出。

    为什么是线程而不是 ``asyncio.wait_for``：信号处理函数在事件循环**之外**
    执行，而且此刻 uvicorn 的关停流程正占着 loop；``threading.Timer`` 只依赖
    解释器。为什么是 ``os._exit``：这是"强制"二字的兑现——checkpoint 已在
    超步边界落盘（``GraphDrained`` 的语义），恢复走启动链，不依赖进程内的
    善后钩子。只在 ``RUN_CONTROL_ENABLED`` 开时被调用。
    """
    global _drain_deadline_timer
    if _drain_deadline_timer is not None or timeout <= 0:
        return

    def _force_exit() -> None:
        logger.error(
            "graceful drain exceeded {}s; forcing exit (checkpoints are resumable)",
            timeout,
        )
        os._exit(1)

    timer = threading.Timer(float(timeout), _force_exit)
    timer.daemon = True
    timer.start()
    _drain_deadline_timer = timer


def _cancel_drain_deadline() -> None:
    global _drain_deadline_timer
    if _drain_deadline_timer is not None:
        _drain_deadline_timer.cancel()
        _drain_deadline_timer = None


def _make_drain_signal_handler(coordinator: Any, previous: Any) -> Any:
    """包一层信号处理器：先请求 drain，再把信号交给原处理器。

    **必须串接**：uvicorn 在 ``Server.serve()`` 里装了自己的处理器（置
    ``should_exit``）。覆盖而不串接，进程就再也不响应 SIGTERM——"优雅关闭"
    会从"停不下来"变成"根本不停"。
    """

    def _handle(signum: int, _frame: Any) -> None:
        try:
            active = coordinator.request_drain(f"signal:{signum}")
            logger.warning(
                "shutdown signal {}: draining {} active run(s) at the next superstep",
                signum,
                active,
            )
            _arm_drain_deadline(settings.RUN_DRAIN_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - 信号处理函数里抛错会吞掉关停信号本身
            logger.warning("drain request failed on signal {}: {}", signum, exc)
        if callable(previous):
            previous(signum, _frame)

    return _handle


def _install_drain_signal_handlers() -> bool:
    """把 SIGTERM/SIGINT 接到 drain 协调器，返回是否至少装上一个。

    开关在函数内判：`RUN_CONTROL_ENABLED` 关 = 直接返回 False，一个处理器都不装
    （调用方无需各自复制判据，纪律 6 ④）。
    """
    if not settings.RUN_CONTROL_ENABLED:
        return False
    if _drain_prev_handlers:
        return True
    from agentix.orchestration.langgraph.policies import get_drain_coordinator

    coordinator = get_drain_coordinator()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous = signal.getsignal(signum)
            signal.signal(signum, _make_drain_signal_handler(coordinator, previous))
        except (ValueError, OSError) as exc:
            # 非主线程 / 平台不支持：fail-open——关停退回「uvicorn 自己退 + 启动恢复」
            # （纪律 2：辅助能力装不上不得阻断启动）。
            logger.warning("drain signal handler not installed for {}: {}", signum, exc)
            continue
        _drain_prev_handlers[int(signum)] = previous
    if _drain_prev_handlers:
        logger.info("drain signal handlers installed: {}", sorted(_drain_prev_handlers))
    return bool(_drain_prev_handlers)


def _uninstall_drain_signal_handlers() -> None:
    for signum, previous in list(_drain_prev_handlers.items()):
        try:
            signal.signal(signum, previous)
        except (ValueError, OSError, TypeError):
            pass
    _drain_prev_handlers.clear()


async def _drain_in_flight_runs() -> None:
    """关停前把在跑的图执行从"被杀"改成"协作式停摆"（``RUN_CONTROL_ENABLED`` 门内）。

    drain 在**超步边界**生效：正在跑的那一步跑完、checkpoint 落盘后抛
    ``GraphDrained``，线程随之可被 ``alist()`` 列出（与 LG-2-1 的启动恢复衔接）。
    等待有上限：超时只告警并继续，上限计时器会兜底强制退出。开关关 = 立即返回。
    """
    if not settings.RUN_CONTROL_ENABLED:
        return
    from agentix.orchestration.langgraph.policies import get_drain_coordinator

    coordinator = get_drain_coordinator()
    active = coordinator.request_drain("shutdown")
    logger.info("graceful drain: {} active run(s) requested to stop", active)
    if await coordinator.wait_for_drain(settings.RUN_DRAIN_TIMEOUT_S):
        # 运行都停了：上限计时器不再有意义（否则会在正常退出的尾巴上误杀）。
        _cancel_drain_deadline()
        logger.info("graceful drain complete")
        return
    logger.warning(
        "graceful drain timed out after {}s ({} run(s) still active); "
        "forcing exit at the deadline",
        settings.RUN_DRAIN_TIMEOUT_S,
        coordinator.active_count(),
    )


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

    # E4：embedding 缓存启动预热（EMBEDDING_CACHE_PREWARM_ENABLED，默认关）。
    # 放在配置校验之后：预热要读 EMBEDDING_* 端点配置，配置本身非法时先让校验那条日志说话。
    # flag 关 ⇒ 一个任务都不创建（纪律 1：默认路径与基线逐位等价，包括"没有多余任务"）。
    from agentix.retrieval.settings import settings as _retrieval_settings

    if _retrieval_settings.EMBEDDING_CACHE_PREWARM_ENABLED:
        global _embedding_prewarm_task
        try:
            _embedding_prewarm_task = asyncio.get_running_loop().create_task(
                _embedding_prewarm_worker(), name="embedding-cache-prewarm"
            )
            logger.info("embedding cache prewarm dispatched (background, non-blocking)")
        except RuntimeError:  # pragma: no cover - 无运行事件循环时跳过
            logger.warning("embedding cache prewarm skipped (no running loop)")

    # Auto-create database tables on startup
    import agentix.infrastructure.persistence.db.models  # noqa: F401 - Import all models to register them with Base
    logger.info("Creating database tables if not exist...")
    Base.metadata.create_all(bind=get_engine())
    # R3.1: 检索域建表（PG 落 retrieval schema / SQLite 同文件）。此前仅在测试与
    # 迁移脚本中调用、启动流程缺失，全新部署会缺表。
    from agentix.retrieval.infra.db.session import create_db_and_tables

    create_db_and_tables()
    logger.info("Database tables ready")

    # P0-1：初始化 LangGraph checkpointer 并（成功时）用 Redis 持久化图
    # 覆盖模块级默认图。降级路径在 init_checkpointer 内部处理（包未装/
    # 显式 memory 配置/Redis 失败 → MemorySaver），此处仅需兜底防御。
    # T22：装配动作收口到 `_wire_graph_checkpointer`——启动与「降级后升级重试」
    # 走同一段重编译逻辑，避免"图该挂哪个 saver"出现两个源（纪律 6 ④）。
    try:
        from agentix.infrastructure.persistence.checkpointer import init_checkpointer

        ckpt = await init_checkpointer()
        _wire_graph_checkpointer(ckpt)
    except Exception as exc:  # noqa: BLE001 - lg_builder.graph 仍为模块级默认图
        logger.error("checkpointer wiring failed, using default in-memory graph: {}", exc)

    # T22：Redis 降级后按可配节奏重试升级（`CHECKPOINTER_RETRY_INTERVAL_S` 默认 0
    # ⇒ 起停函数第一道判定直接返回：不建任务、不打日志，与基线逐位等价）。
    # **放在装配 try 之外**：装配失败恰恰是最该把重试跑起来的形态，塞进 try 里
    # 会让一次装配异常连带把唯一的自愈路径也跳过。
    _start_checkpointer_upgrade()

    # Phase 16：HITL 三栈桥接线（**仅 flag 开启时**）。默认
    # HUMAN_REVIEW_ENABLED=false ⇒ 不接线 ⇒ ToolExecutor 保持
    # hook=None 的零变化路径（Strangler 字节等价）。
    # T19：`bridge` 在块外先置 None，扫描任务的**起停判定**才能统一收口在
    # `_start_hitl_deadline_sweeper` 一处（纪律 6 ④）——install 失败时它收到 None，
    # 自己告警并返回 False；这里若把整段调用都塞进 try，失败时连"配了却没生效"
    # 都不会有人告警。
    bridge: Any = None
    if bool(getattr(settings, "HUMAN_REVIEW_ENABLED", False)):
        try:
            from agentix.policies.hitl.hitl_bridge import install_executor_hook

            bridge = install_executor_hook()
            logger.info("HITL bridge hook installed (HUMAN_REVIEW_ENABLED=true)")
        except Exception as exc:  # noqa: BLE001 - 接线失败不影响启动（闸门仍 fail-closed）
            logger.error("HITL bridge hook install failed: {}", exc)
    # T19：审批等待时限扫描（`HITL_APPROVAL_DEADLINE_S` 默认 0 ⇒ 不建任务）。
    # **起在块外**：三件事各自成立才该起——deadline>0、HUMAN_REVIEW 开、持久化开、
    # 桥真装上（四道判定与告警全在 `_start_hitl_deadline_sweeper` 里）。放在块内，
    # hook 安装失败就再没人能告警"deadline 配了却没人扫"；放在块外，flag-off
    # （HUMAN_REVIEW=false 且 deadline=0）走第一道判定即返回：不建任务、不打日志。
    _start_hitl_deadline_sweeper(bridge)

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

    # P2（代码执行）：code_execute 工具只在 CODE_EXEC_ENABLED=true 时注册
    # （默认关 ⇒ 零注册，与基线逐位等价）。声明的隔离档位是 subprocess（POSIX 专属），
    # 平台不支持时执行点 fail-closed 拒绝，不静默退回进程内。
    if bool(getattr(settings, "CODE_EXEC_ENABLED", False)):
        try:
            from agentix.application.services.code_exec_bridge import (
                install_code_execute_tool,
            )

            install_code_execute_tool()
            logger.info("code execute tool installed (CODE_EXEC_ENABLED=true)")
        except Exception as exc:  # noqa: BLE001 - 接线失败不影响启动
            logger.error("code execute tool install failed: {}", exc)

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

    # LG-3-6：优雅关闭信号（`RUN_CONTROL_ENABLED` 关 = 安装函数内部直接返回，
    # 一个处理器都不装）。装在崩溃恢复之前：恢复跑的是图执行，若此刻收到
    # SIGTERM，也要能被 drain 覆盖。
    _install_drain_signal_handlers()

    # T26：Redis Stream 队列消费 worker（`TASK_QUEUE_WORKER_ENABLED` 默认关 ⇒
    # 第一道判定直接返回：不建任务、不建连、不打日志、不 import 队列模块）。
    # **位置理由**：① 排在所有装配块之后——消费一旦开始就会调业务 handler（检索 /
    # LLM / 工具链），此刻它们必须已经装好；② 排在 `_install_drain_signal_handlers()`
    # 之后——信号处理器先就位，消费期间收到 SIGTERM 才走得到 drain，否则这段时间是
    # 关停盲区；③ 排在 `_startup_crash_recovery()` 之前——恢复最多占 30 秒预算
    # （`RECOVERY_TOTAL_BUDGET_S`），把可并行推进的消费排在它后面等于白白晚启动半分钟。
    _start_task_queue_worker()

    # Crash recovery: resume any interrupted threads
    await _startup_crash_recovery()


async def _shutdown() -> None:
    """应用退出逻辑（企业化 P2-1.1：由 lifespan 协议驱动）。"""
    logger.info("Shutting down {}", settings.APP_NAME)

    # LG-3-6：先把在跑的图执行 drain 掉（`RUN_CONTROL_ENABLED` 关 = 立即返回）。
    # 放在最前面：其它清理（cron / flush / 连接池）都不该占用 drain 的等待预算。
    try:
        await _drain_in_flight_runs()
    except Exception as exc:  # noqa: BLE001 - drain 失败退回「直接退出 + 启动恢复」
        logger.warning("graceful drain skipped: {}", exc)

    # T26：队列消费 worker 收口。**放在 drain 之后、其它清理之前**：drain 管的是
    # 「已经在跑的图执行」，worker 管的是「还会接新活」的消费循环——顺序反了会在
    # drain 那段时间里继续把新任务接进来，让「停机中」变成「边停边干」。
    #
    # **与 `RUN_DRAIN_TIMEOUT_S` 互不占用**：`stop_worker()` 只把 `_running` 置
    # False，当下那次 `xreadgroup(block=5000)` 仍要自然到期才返回（最多 5 秒，
    # `start_worker` 的 `block_ms` 默认值），这段等待**不**进 drain 的预算、也不受
    # 它约束；紧接着的 `cancel()` 直接打断该等待，所以实际只多一次取消点调度。
    # 因此本段不需要（也不该复用）graceful timeout 口径：worker 收口没有第二个上限。
    global _task_queue_worker_task, _task_queue_worker
    if _task_queue_worker_task is not None:
        if _task_queue_worker is not None:
            try:
                await _task_queue_worker.stop_worker()
            except Exception as exc:  # noqa: BLE001 - 关停清理失败只留痕，不阻断后续清理（下面仍会 cancel）
                logger.warning("task queue worker stop failed: {}", exc)
        _task_queue_worker_task.cancel()
        try:
            with contextlib.suppress(asyncio.CancelledError):
                await _task_queue_worker_task
        except Exception as exc:  # noqa: BLE001 - 关停清理失败只留痕，不阻断后续清理
            logger.warning("task queue worker shutdown failed: {}", exc)
        finally:
            _task_queue_worker_task = None
            _task_queue_worker = None

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

    # E4：预热是 fire-and-forget。关停时不取消，正在批量打端点的那条线程就成了
    # 关停悬挂的一个来源（to_thread 的线程不可强杀，但取消任务至少不再往里等）。
    global _embedding_prewarm_task
    if _embedding_prewarm_task is not None:
        _embedding_prewarm_task.cancel()
        _embedding_prewarm_task = None

    # T19：审批时限扫描任务。不取消它，关停期间它还会继续读库、写库并把刚被
    # 裁决的 pending 拖成"未知来源的裁决"——与 drain 抢连接也毫无意义。
    # CancelledError 必须收口（它是 BaseException，漏出去会把整个关停打断）。
    global _hitl_deadline_task, _hitl_deadline_bridge
    if _hitl_deadline_task is not None:
        _hitl_deadline_task.cancel()
        try:
            with contextlib.suppress(asyncio.CancelledError):
                await _hitl_deadline_task
        except Exception as exc:  # noqa: BLE001 - 关停清理失败只留痕，不阻断后续清理
            logger.warning("HITL approval deadline sweeper shutdown failed: {}", exc)
        finally:
            _hitl_deadline_task = None

    # T19 第 2 轮返工：扫描租约是**进程间**的锁。进程走了不让出，另一个 worker
    # 得等 TTL（120 秒）到期才接手——那段时间里到期 pending 没人扫，超时语义
    # 静默失效。只释放已持有的那台桥（模块级句柄），不重新构造单例。
    if _hitl_deadline_bridge is not None:
        try:
            await asyncio.to_thread(_hitl_deadline_bridge.release_sweep_lease)
        except Exception as exc:  # noqa: BLE001 - 关停清理失败只留痕；租约到期自愈，不阻断后续清理
            logger.warning("HITL sweep lease release on shutdown failed: {}", exc)
        finally:
            _hitl_deadline_bridge = None

    # T22：checkpointer 升级重试任务。它可能正卡在 asyncio.sleep 或一次 Redis 建连里；
    # 不取消就留了一条"关停期间还在建连"的后台链，既与 drain 抢连接，也让关停多一个
    # 悬挂来源（to_thread / 建连不可强杀，取消任务至少不再往里等）。
    global _checkpointer_upgrade_task
    if _checkpointer_upgrade_task is not None:
        _checkpointer_upgrade_task.cancel()
        try:
            with contextlib.suppress(asyncio.CancelledError):
                await _checkpointer_upgrade_task
        except Exception as exc:  # noqa: BLE001 - 关停清理失败只留痕，不阻断后续清理
            logger.warning("checkpointer upgrade retry shutdown failed: {}", exc)
        finally:
            _checkpointer_upgrade_task = None

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

    # LG-3-6：关停走完，把信号处理器还原（`signal.signal` 是进程级状态）。
    _uninstall_drain_signal_handlers()


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
    # G6：reload 与 DEBUG 解耦；UVICORN_RELOAD 未显式设置时沿用旧行为（=DEBUG）。
    _reload = settings.DEBUG if settings.UVICORN_RELOAD is None else settings.UVICORN_RELOAD
    # LG-3-6：`python -m agentix.main` 的关停等待也给上限（容器里走 uvicorn CLI，
    # 那条路径由信号处理器起的 `os._exit` 计时线程兜底，见 `_arm_drain_deadline`）。
    _graceful_kwargs = (
        {"timeout_graceful_shutdown": max(1, int(settings.RUN_DRAIN_TIMEOUT_S))}
        if settings.RUN_CONTROL_ENABLED
        else {}
    )
    uvicorn.run(
        "agentix.main:application",
        host=settings.HOST,
        port=settings.PORT,
        reload=_reload,
        log_level="info",
        **_graceful_kwargs,
    )

# Backwards-compatible alias for tooling expecting `app` symbol
app = application
