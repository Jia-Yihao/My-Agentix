"""可靠性 - LLM 全局并发限制器（企业化 P1-5.3）。

问题：LLM 上游配额有限，高峰期无显式并发上限时会把配额打满，
所有在途请求同时变慢 → 超时重试放大 → 全站雪崩。

方案：进程级 asyncio.Semaphore 排队 + 排队超时快速失败。
- ``LLM_MAX_CONCURRENCY=0``（默认）→ 直通零开销，行为与改造前一致；
- ``>0`` → 在途调用超限时排队，等待超过 ``LLM_CONCURRENCY_QUEUE_TIMEOUT``
  秒则抛 :class:`LLMConcurrencyLimitError`（调用方按结构化错误返回 503）。

与限流（rate_limiter）的分工：限流管"每分钟能来多少请求"（入口侧），
并发限制管"同时在跑多少 LLM 调用"（出口侧）。两者互补，缺一不可。

多副本部署说明：信号量是进程级的，多副本时全局上限 ≈ N 副本 ×
LLM_MAX_CONCURRENCY。需要精确全局上限时切换 Redis（接口预留，
与 RateLimiter 的 RedisRateLimiter 同一演进路径）。

依赖方向：reliability 共享层，只依赖 stdlib + observability（延迟导入、
失败安全），可被 application / retrieval 两域安全复用。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

logger = logging.getLogger(__name__)


class LLMConcurrencyLimitError(Exception):
    """LLM 全局并发排队超时（调用方应转 503 + Retry-After，或走降级响应）。"""

    def __init__(self, max_concurrency: int, waited_seconds: float):
        self.max_concurrency = max_concurrency
        self.waited_seconds = waited_seconds
        super().__init__(
            f"LLM concurrency limit reached (max={max_concurrency}), "
            f"queued for {waited_seconds:.1f}s without a slot"
        )


class GlobalConcurrencyLimiter:
    """进程级全局并发限制（信号量 + 排队超时）。

    用法：
        async with llm_limiter.acquire():
            resp = await llm.ainvoke(prompt)
    """

    def __init__(self, max_concurrency: int, queue_timeout: float = 5.0):
        self._max = max(0, int(max_concurrency))
        self._queue_timeout = max(0.0, float(queue_timeout))
        self._semaphore: asyncio.Semaphore | None = (
            asyncio.Semaphore(self._max) if self._max > 0 else None
        )

    @property
    def enabled(self) -> bool:
        """是否启用限制（max_concurrency=0 时直通）。"""
        return self._semaphore is not None

    @property
    def max_concurrency(self) -> int:
        return self._max

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        """占一个并发槽位；未启用 / 超时快速失败的语义见模块 docstring。"""
        if self._semaphore is None:
            yield
            return

        from agentix.observability.metrics import (
            record_llm_concurrency_occupied,
            record_llm_concurrency_rejected,
            record_llm_concurrency_released,
        )

        # 排队等待（超时则快速失败，不无限堆积）
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(), timeout=self._queue_timeout
            )
        except asyncio.TimeoutError as exc:
            record_llm_concurrency_rejected()
            logger.warning(
                "LLM concurrency queue timeout (max=%s, waited=%.1fs)",
                self._max,
                self._queue_timeout,
            )
            raise LLMConcurrencyLimitError(self._max, self._queue_timeout) from exc

        try:
            record_llm_concurrency_occupied()
            yield
        finally:
            record_llm_concurrency_released()
            self._semaphore.release()


def build_llm_limiter() -> GlobalConcurrencyLimiter:
    """按 settings 构造 LLM 并发限制器（测试可直接传参构造，绕过本函数）。"""
    from agentix.config.settings import settings

    return GlobalConcurrencyLimiter(
        max_concurrency=settings.LLM_MAX_CONCURRENCY,
        queue_timeout=settings.LLM_CONCURRENCY_QUEUE_TIMEOUT,
    )


# 进程级单例（懒初始化，避免 import 期读 settings）
_llm_limiter: GlobalConcurrencyLimiter | None = None


def get_llm_limiter() -> GlobalConcurrencyLimiter:
    """全局 LLM 并发限制器单例。"""
    global _llm_limiter
    if _llm_limiter is None:
        _llm_limiter = build_llm_limiter()
    return _llm_limiter


def reset_llm_limiter() -> None:
    """重置单例（仅供测试）。"""
    global _llm_limiter
    _llm_limiter = None
