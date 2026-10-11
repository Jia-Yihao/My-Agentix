"""企业化 P1-5.3：LLM 全局并发限制器测试。

覆盖：直通模式 / 排队放行 / 排队超时快速失败 / Gauge 占用计数 /
invoke_with_breaker 接线（检索域 LLM 咽喉点）。
"""

from __future__ import annotations

import asyncio

import pytest

from agentix.reliability.concurrency_limiter import (
    GlobalConcurrencyLimiter,
    LLMConcurrencyLimitError,
    get_llm_limiter,
    reset_llm_limiter,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_llm_limiter()
    yield
    reset_llm_limiter()


def _scrape(metric_substr: str) -> str:
    from agentix.observability.metrics import get_metrics

    return "\n".join(
        line
        for line in get_metrics().decode("utf-8").splitlines()
        if metric_substr in line
    )


def test_disabled_passthrough_zero_overhead():
    """max=0（默认）：直通，不抛错不排队。"""

    async def _run():
        limiter = GlobalConcurrencyLimiter(max_concurrency=0)
        assert limiter.enabled is False
        async with limiter.acquire():
            pass  # 直通

    asyncio.run(_run())


def test_queueing_allows_sequential_overflow():
    """max=1：第二个调用排队等待第一个释放后正常执行。"""

    async def _run():
        limiter = GlobalConcurrencyLimiter(max_concurrency=1, queue_timeout=5.0)
        results: list[str] = []

        async def _worker(tag: str, hold: float):
            async with limiter.acquire():
                results.append(f"{tag}:enter")
                await asyncio.sleep(hold)
                results.append(f"{tag}:exit")

        await asyncio.gather(_worker("a", 0.05), _worker("b", 0.05))

        # 必须成对串行（不允许两个同时在槽内）
        assert results.index("a:exit") < results.index("b:enter") or results.index(
            "b:exit"
        ) < results.index("a:enter")

    asyncio.run(_run())


def test_queue_timeout_fails_fast():
    """排队超时：槽被占满时新调用快速失败，不无限堆积。"""

    async def _run():
        limiter = GlobalConcurrencyLimiter(max_concurrency=1, queue_timeout=0.05)

        async def _holder():
            async with limiter.acquire():
                await asyncio.sleep(0.3)

        holder_task = asyncio.create_task(_holder())
        await asyncio.sleep(0.01)  # 让 holder 先拿到槽

        with pytest.raises(LLMConcurrencyLimitError):
            async with limiter.acquire():
                pass

        await holder_task

    asyncio.run(_run())


def test_gauge_occupancy_tracked():
    """并发占用 Gauge：进入置 1 / 退出归零。"""

    async def _run():
        limiter = GlobalConcurrencyLimiter(max_concurrency=2)
        async with limiter.acquire():
            scraped = _scrape("llm_concurrency")
            value_line = next(
                line
                for line in scraped.splitlines()
                if line.startswith("llm_concurrency ")
            )
            assert value_line.rsplit(" ", 1)[1] == "1.0"

        # 退出后 Gauge 应回 0
        scraped = _scrape("llm_concurrency")
        value_line = next(
            line for line in scraped.splitlines() if line.startswith("llm_concurrency ")
        )
        assert value_line.rsplit(" ", 1)[1] == "0.0"

    asyncio.run(_run())


def test_singleton_reads_settings(monkeypatch):
    """单例按 settings 构造（max=0 → 直通）。"""
    from agentix.config.settings import settings

    monkeypatch.setattr(settings, "LLM_MAX_CONCURRENCY", 0)
    limiter = get_llm_limiter()
    assert limiter.enabled is False
    assert limiter is get_llm_limiter()  # 单例


def test_invoke_with_breaker_goes_through_limiter(monkeypatch):
    """检索域咽喉点接线：LLM_CIRCUIT_BREAKER_ENABLED=false 路径也过并发限制器。"""
    from agentix.retrieval.settings import settings as retrieval_settings
    from agentix.retrieval.usecases import chat_llm

    monkeypatch.setattr(retrieval_settings, "LLM_CIRCUIT_BREAKER_ENABLED", False)
    from agentix.config.settings import settings as main_settings

    monkeypatch.setattr(main_settings, "LLM_MAX_CONCURRENCY", 1)
    monkeypatch.setattr(main_settings, "LLM_CONCURRENCY_QUEUE_TIMEOUT", 1.0)

    class _FakeLLM:
        def invoke(self, prompt):  # 同步接口（to_thread 执行）
            return f"ok:{prompt}"

    async def _run():
        result = await chat_llm.invoke_with_breaker(_FakeLLM(), "ping")
        assert result == "ok:ping"

    asyncio.run(_run())
