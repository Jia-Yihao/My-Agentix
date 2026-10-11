
import logging

from fastapi import FastAPI

from kb_service.api.deps import get_config
from kb_service.api.routes import router as api_router

logger = logging.getLogger(__name__)


def _report_auth_mode() -> None:
    """启动期显式报告鉴权状态。

    为什么必须"说出来"：本服务此前 5 个端点**全部无鉴权**，而 compose 把
    ``8100:8000`` 发布到宿主、uvicorn 又 ``--host 0.0.0.0``。**"没有鉴权"这件事
    本身没有任何日志**——运维看不到、审计也查不到，只有读代码才知道。
    agentix 侧早就有同样的 self-report（其 ``main.py`` 里的
    "API key auth is DISABLED"），本服务补上以对齐口径。

    这里只报告、不阻断：``KB_REQUIRE_API_KEY`` 默认 false 是为了不改变既有部署
    行为；阻断会让所有现存 ``docker compose up`` 直接起不来。
    """
    config = get_config()
    if not config.require_api_key:
        logger.warning(
            "kb_ingest auth is DISABLED (KB_REQUIRE_API_KEY=false): all endpoints are open. "
            "This service publishes 8100:8000 to the host and listens on 0.0.0.0 — "
            "restrict it to a trusted network, or set KB_REQUIRE_API_KEY=true plus KB_API_KEY "
            "to require the X-API-Key header."
        )
    elif not config.api_key:
        # 开启鉴权但没配密钥 ⇒ 没有任何客户端能通过（fail-closed）。
        # 这是"锁死"而不是"安全"，必须显式告警，否则现场只会看到 401 满天飞。
        logger.error(
            "KB_REQUIRE_API_KEY=true but KB_API_KEY is empty: every request will be rejected "
            "with 401 (fail-closed). Set KB_API_KEY to the shared secret."
        )
    else:
        logger.info("kb_ingest auth enabled: X-API-Key required on all /api endpoints")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Knowledge Ingestion Service",
        version="0.1.0",
        description="Rewrite tabular data with LLMs, embed into Milvus, and expose retrieval APIs.",
    )
    # expose both legacy /api and the expected /api/v1/knowledge paths
    app.include_router(api_router, prefix="/api")
    app.include_router(api_router, prefix="/api/v1/knowledge")

    @app.get("/health", tags=["system"])
    def health_check():
        return {"status": "ok"}

    _report_auth_mode()
    logger.info("FastAPI application initialised")
    return app


app = create_app()
