"""出口隔离舱（按下游分池 semaphore，roadmap E3）。

池尺寸走既有 settings 模式（``0 = 不限`` 默认 = 接线前逐位等价）：

- ``BULKHEAD_RETRIEVAL_SIZE`` —— 检索 miss 计算缝（chat_service 检索级联）；
- ``BULKHEAD_NEO4J_SIZE`` —— Cypher 工具执行缝（executor 按工具名归池）；
- ``BULKHEAD_MCP_SIZE`` —— MCP stdio 下游（``MCPStdioClient.call_tool``）；
- ``BULKHEAD_WEB_SEARCH_SIZE`` —— 外部搜索工具（executor 按工具名归池）。

LLM 池**不另设**：既有 ``get_llm_limiter()``（``LLM_MAX_CONCURRENCY``）+
熔断器已是 LLM 下游的准入/隔离面，再叠一层只会双重排队。

故障不串扰：某池打满只阻塞归入该池的调用方；池获取走 ``asyncio.Semaphore``
（无锁队列饥饿问题），信号量按名字惰性构建、``reset_bulkheads()`` 供测试隔离。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Optional

from agentix.config import settings

__all__ = [
    "bulkhead_slot",
    "pool_for_tool",
    "pooled_wait_for",
    "reset_bulkheads",
]

#: 工具名 → 下游池（executor 执行缝按此归池；未列出的工具不入池）。
TOOL_POOL_MAP: Dict[str, str] = {
    "search": "web_search",
    "cypher_query": "neo4j",
    "predefined_cypher": "neo4j",
}

_POOL_SIZE_KEYS = {
    "retrieval": "BULKHEAD_RETRIEVAL_SIZE",
    "neo4j": "BULKHEAD_NEO4J_SIZE",
    "mcp": "BULKHEAD_MCP_SIZE",
    "web_search": "BULKHEAD_WEB_SEARCH_SIZE",
}

_pools: Dict[str, asyncio.Semaphore] = {}


def _pool_size(name: str) -> int:
    key = _POOL_SIZE_KEYS.get(name)
    if key is None:
        return 0
    try:
        return int(getattr(settings, key, 0) or 0)
    except Exception:  # noqa: BLE001 - 配置异常按不限处理（不打挂调用）
        return 0


def _get_pool(name: str) -> Optional[asyncio.Semaphore]:
    """池信号量（size<=0 → None = 不限；惰性构建，尺寸读当前 settings）。"""
    size = _pool_size(name)
    if size <= 0:
        return None
    pool = _pools.get(name)
    if pool is None:
        pool = asyncio.Semaphore(size)
        _pools[name] = pool
    return pool


def reset_bulkheads() -> None:
    """测试隔离：丢弃全部池信号量（下一次进入按当前 settings 重建）。"""
    _pools.clear()


def pool_for_tool(tool_name: str) -> Optional[str]:
    """工具名归池；未配置（size 0）或不归池 → None。"""
    name = TOOL_POOL_MAP.get(tool_name)
    if name is None:
        return None
    if _pool_size(name) <= 0:
        return None
    return name


@asynccontextmanager
async def bulkhead_slot(pool: str) -> AsyncIterator[None]:
    """获取池位（size<=0 直通零开销）；满则等待（隔离舱语义）。"""
    sem = _get_pool(pool)
    if sem is None:
        yield
        return
    async with sem:
        yield


async def pooled_wait_for(coro, timeout_s: float, pool: Optional[str]) -> object:
    """``wait_for`` + 可选池位（W3 executor 执行缝的统一入口）。

    池位只包住**单次尝试**（退避等待不占池）；超时/异常经 wait_for 原样
    抛出，CM 在异常路径正常释放池位。
    """
    if pool is None:
        return await asyncio.wait_for(coro, timeout=timeout_s)
    async with bulkhead_slot(pool):
        return await asyncio.wait_for(coro, timeout=timeout_s)
