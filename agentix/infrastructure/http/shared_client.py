"""共享 ``httpx.AsyncClient`` 生命周期（消灭 per-request 创建）。

``SHARED_HTTP_MAX_CONNECTIONS``（默认 0 = 关）：

- ``<=0``：每次请求新建 + ``aclose``（与基线逐位等价——调用方经
  :func:`acquire_http_client` 拿到独立 client，``owns=True``）；
- ``>0``：进程级共享 client（连接池上限 = 该值，惰性构建、关闭即失效可
  重建），``owns=False``——**调用方不得 aclose**，超时必须在请求级
  ``timeout=`` 传入（共享 client 不带 per-request 超时语义）。

应用关停经 ``aclose_shared_async_client()``（lifespan shutdown；未构建则
零开销）。测试用 ``reset_shared_async_client()`` 隔离。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Tuple

import httpx

from agentix.config import settings

__all__ = [
    "acquire_http_client",
    "get_shared_async_client",
    "aclose_shared_async_client",
    "reset_shared_async_client",
]

_shared: httpx.AsyncClient | None = None


def get_shared_async_client() -> httpx.AsyncClient | None:
    """取进程级共享 client（flag 关 / 未构建且已关 → None）。"""
    global _shared
    limit = int(getattr(settings, "SHARED_HTTP_MAX_CONNECTIONS", 0) or 0)
    if limit <= 0:
        return None
    if _shared is None or _shared.is_closed:
        _shared = httpx.AsyncClient(
            # trust_env=False：与 redis embedding 基线一致（不读环境代理，
            # 出口行为确定性优先；rerank 原 per-request 默认 trust_env=True，
            # flag 开启时按共享口径统一——偏差已记规划 §9）。
            trust_env=False,
            limits=httpx.Limits(
                max_connections=limit,
                max_keepalive_connections=max(1, limit // 2),
            )
        )
    return _shared


@asynccontextmanager
async def acquire_http_client(
    *, timeout: float, **client_kwargs: Any
) -> AsyncIterator[Tuple[httpx.AsyncClient, bool]]:
    """进入 HTTP 客户端上下文，产出 ``(client, owns)``。

    ``owns=True`` → 退出时由本 CM 关闭（per-request 基线）；
    ``owns=False`` → 共享实例，CM 不关。调用方无论何种模式都应把超时
    以 ``timeout=`` 传给具体请求（共享模式下客户端级 timeout 不可靠）。
    """
    shared = get_shared_async_client()
    if shared is not None:
        yield shared, False
        return
    async with httpx.AsyncClient(timeout=timeout, **client_kwargs) as client:
        yield client, True


async def aclose_shared_async_client() -> None:
    """关停钩子（lifespan shutdown）；未构建时零开销。"""
    global _shared
    client, _shared = _shared, None
    if client is not None and not client.is_closed:
        await client.aclose()


def reset_shared_async_client() -> None:
    """测试隔离：丢弃引用（不 await 关闭——测试内 client 无连接池压力）。"""
    global _shared
    _shared = None
