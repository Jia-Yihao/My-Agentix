
import logging
import secrets
from functools import lru_cache
from typing import Optional

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from kb_service.core.config import Config, load_config

logger = logging.getLogger(__name__)

#: 与 agentix 侧**同名**（两边都用 ``X-API-Key``），但**密钥各用各的**：
#: 本服务读 ``KB_API_KEY``，不读主应用的 ``API_KEY``（理由见下）。
#: 用 ``APIKeyHeader`` 而不是裸 ``Header``，这样 ``/docs`` 会出现 "Authorize"
#: 输入框，联调时不必手写请求头。``auto_error=False``：缺头时**由我们自己**
#: 抛 401（保持与 agentix 相同的响应体形状
#: ``{"detail": "Invalid or missing API key"}``），而不是让 FastAPI 抛默认的 403。
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@lru_cache
def get_config() -> Config:
    """Provide a shared configuration instance for the application."""
    return load_config()


async def require_api_key(x_api_key: Optional[str] = Security(_api_key_header)) -> str:
    """校验 ``X-API-Key`` 请求头。

    与 ``agentix.infrastructure.core.security.verify_api_key`` **头名相同、
    密钥不同**：同样用 ``X-API-Key``、同样返回 401 ``Invalid or missing API key``，
    但读的是本服务自己的 ``KB_REQUIRE_API_KEY`` / ``KB_API_KEY``。

    ⚠️ **为什么刻意不复用主应用的 ``API_KEY``**（首版我复用了，那是错的）：
    本容器的 compose ``env_file`` 就是仓库根 ``.env``，复用同一个键意味着
    "任何拿到对外 API Key 的外部客户端"都能直接调本服务的**写入**端点
    （改 Milvus 集合）——把对外凭证变成了对内写权限，属**权限抬升**。
    两者信任边界不同：主应用的 Key 面向外部调用方，本服务的 Key 面向
    内部调用方（agentix）。故各用各的，命名沿用该服务既有的 ``KB_*`` 约定。

    为什么这个服务必须有鉴权（此前**一个都没有**）：
      * compose 里是 ``ports: ${KB_INGEST_PORT:-8100}:8000`` —— 端口**发布到宿主机**；
      * 启动命令是 ``uvicorn ... --host 0.0.0.0`` —— 监听**所有网卡**；
      * 端点是**写入**语义（Excel / MySQL 入库，直接改 Milvus 集合）；
      * 同服务的 ``vector_search`` / ``hybrid_search`` 还会把知识库内容读出去。
      四者叠加 = 同一网段内任何人可无凭证改写知识库、读取全部数据条目语料。

    ``KB_REQUIRE_API_KEY`` 默认为 **false**，以保持既有部署行为；但启动期会
    **大声告警**（见 ``main._report_auth_mode``），不至于像此前那样完全无声。

    用常量时间比较（``secrets.compare_digest``）而非 ``==``：避免按字符短路
    泄露密钥前缀。
    """
    config = get_config()

    if not config.require_api_key:
        return ""

    expected = config.api_key
    # ``not expected`` 也要拦：开启鉴权但没配密钥时，若只判 ``x_api_key`` 真假，
    # 一个空字符串头就会通过。宁可全部 401（fail-closed），并在启动期告警。
    if not x_api_key or not expected or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return x_api_key
