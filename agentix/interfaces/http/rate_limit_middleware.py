"""全局限流中间件（企业化 P0-3）。

背景（企业级评估 §4.1）：`reliability/rate_limiter.py` 的令牌桶此前只挂在
知识域聊天路由（`retrieval/api/routers/chat.py`），测试数据编排域全部路由
（interfaces/http）未覆盖——单客户端可打爆主链路。本中间件把限流收敛为
**全局 ASGI 中间件**，统一覆盖所有入口。

设计要点：
- **复用既有 RateLimiter**（reliability/rate_limiter.py 令牌桶），不重造轮子；
- **按客户端 IP 维度限流**（多副本部署时应切 RedisRateLimiter，接口已预留）；
- 健康探针与监控面路径豁免——清单 import 自 `public_paths.py`，与鉴权中间件共用同一对象；
- 触发限流时返回 429 + Retry-After，并打 `rate_limit_triggered_total{scope="global"}`；
- Redis 故障时放行（与 RedisRateLimiter 既有语义一致），可用性优先。
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from agentix.interfaces.http.public_paths import PUBLIC_PATHS, PUBLIC_PREFIXES
from agentix.reliability.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# 豁免路径与鉴权中间件共用同一份清单（`public_paths.py`）。此前这里另抄了一份、
# 靠注释宣称"同口径"——两处各写一份判据，迟早只改一处。

# 单实例最多跟踪的限流键数量（防恶意伪造 IP 撑爆内存）；
# 超限后整体清空（令牌桶状态可丢弃，短暂突发窗口可接受）。
_MAX_TRACKED_KEYS = 10_000

# 上传接口单独配额（技术变更与架构演进指南 v2.0 §2.1.5②）：
# 上传是重 IO + 触发索引任务的重路径，配额应显著低于普通请求。
# 前缀匹配；命中后走独立令牌桶（UPLOAD_*），不受全局 RPM 配额掩护。
_UPLOAD_PATH_PREFIXES = (
    "/api/v1/upload",  # 编排域：文件/图片上传
    "/api/v1/retrieval/documents",  # 检索域：文档上传（含批量）
)


def _client_ip(scope: dict) -> str:
    """提取客户端 IP：优先 X-Forwarded-For 首跳，退回 scope.client。"""
    headers = dict(scope.get("headers") or [])
    forwarded = headers.get(b"x-forwarded-for", b"").decode("utf-8", errors="ignore")
    if forwarded:
        first_hop = forwarded.split(",")[0].strip()
        if first_hop:
            return first_hop
    client = scope.get("client") or ("unknown", 0)
    return client[0] if isinstance(client, (tuple, list)) else str(client)


class GlobalRateLimitMiddleware:
    """全局令牌桶限流中间件（纯 ASGI，与 main.py 既有中间件风格一致）。

    Args:
        app: 下游 ASGI 应用。
        rpm: 每分钟稳态请求数（令牌补充速率 = rpm/60）。
        burst: 令牌桶突发容量。
        enabled: 是否启用（关闭时直接放行，便于灰度）。
    """

    def __init__(
        self,
        app,
        rpm: int = 120,
        burst: int = 240,
        enabled: bool = False,
        upload_rpm: int = 20,
        upload_burst: int = 30,
    ):
        self.app = app
        self._rpm = max(1, rpm)
        self._burst = max(1, burst)
        # 上传单独配额（指南 §2.1.5②）：默认 20 req/min、burst 30
        self._upload_rpm = max(1, upload_rpm)
        self._upload_burst = max(1, upload_burst)
        self._enabled = enabled
        self._limiters: Dict[str, RateLimiter] = {}

    def _limiter_for(self, key: str, *, rpm: int | None = None, burst: int | None = None) -> RateLimiter:
        limiter = self._limiters.get(key)
        if limiter is None:
            if len(self._limiters) >= _MAX_TRACKED_KEYS:
                logger.warning("Rate limiter key table full (%d), resetting", _MAX_TRACKED_KEYS)
                self._limiters.clear()
            # 令牌桶：每秒补充 rpm/60 个令牌，桶容量 burst
            _rpm = (rpm or self._rpm)
            _burst = (burst or self._burst)
            limiter = RateLimiter(rate=_rpm / 60.0, burst=_burst)
            self._limiters[key] = limiter
        return limiter

    @staticmethod
    def _is_exempt(path: str) -> bool:
        return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self._enabled:
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if self._is_exempt(path):
            return await self.app(scope, receive, send)

        key = _client_ip(scope)
        is_upload = path.startswith(_UPLOAD_PATH_PREFIXES)
        if is_upload:
            # 上传单独配额：独立桶（key 前缀区分，避免与普通桶互相顶替）
            limiter = self._limiter_for(
                f"upload:{key}", rpm=self._upload_rpm, burst=self._upload_burst
            )
        else:
            limiter = self._limiter_for(key)

        if await limiter.acquire():
            return await self.app(scope, receive, send)

        # 限流触发：429 + Retry-After + 指标
        from agentix.observability.metrics import record_rate_limit_triggered

        record_rate_limit_triggered(scope="upload" if is_upload else "global")
        retry_after = max(1, int(limiter.retry_after()) or 1)
        response = _json_response(
            status_code=429,
            headers={"Retry-After": str(retry_after)},
            content={
                "error": "Too Many Requests",
                "message": "Rate limit exceeded; slow down and retry shortly",
                "retry_after_seconds": retry_after,
            },
        )
        return await response(scope, receive, send)


def _json_response(status_code: int, content: dict, headers: Optional[dict] = None):
    """最小 JSON 响应构造（避免在模块顶层引入 fastapi 造成循环导入）。"""
    from starlette.responses import JSONResponse

    return JSONResponse(status_code=status_code, content=content, headers=headers)
