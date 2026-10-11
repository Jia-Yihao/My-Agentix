"""企业化 P1 观测补全测试（P1-2.1 降级 / P1-2.2 熔断 / P1-5.1 编排域 token 计量）。

验证点：
1. record_degraded 助手把 degraded_total{component} 写进 /metrics 抓取面；
2. 熔断器状态转换会同步 Gauge 与转换事件 Counter（OPEN=2 / CLOSED=0）；
3. 编排域 _TrackedChatOpenAI.ainvoke 把 token 用量上报 Prometheus
   （检索域 M6.3 已覆盖，此前编排域只进 Redis 不进指标——P1-5.1 补齐）。
"""

from __future__ import annotations

import asyncio
import types

import pytest

from agentix.observability.metrics import get_metrics, record_degraded
from agentix.reliability.circuit_breaker import CircuitBreaker, CircuitState


def _scrape(metric_substr: str) -> str:
    """抓取 /metrics 文本并过滤出包含指定指标名的行。"""
    return "\n".join(
        line for line in get_metrics().decode("utf-8").splitlines() if metric_substr in line
    )


def test_record_degraded_exports_counter():
    """P1-2.1：降级事件必须出现在 /metrics 抓取面（此前只可查询不上报）。"""
    record_degraded("checkpointer_test")

    scraped = _scrape("degraded_total")
    assert "degraded_total" in scraped
    assert 'component="checkpointer_test"' in scraped


def test_circuit_breaker_transitions_export_gauge_and_counter():
    """P1-2.2：熔断打开/恢复要反映到 circuit_state Gauge 与转换计数。"""
    breaker = CircuitBreaker(name="p1_test_breaker", failure_threshold=2)

    async def _boom():
        raise RuntimeError("upstream down")

    async def _drive():
        for _ in range(2):  # 连续失败 2 次 → OPEN
            with pytest.raises(RuntimeError):
                await breaker.call(_boom)
        assert breaker.state is CircuitState.OPEN

        scraped_state = _scrape("circuit_state")
        assert 'name="p1_test_breaker"' in scraped_state
        # OPEN=2（closed=0 half_open=1 open=2）
        assert _gauge_value(scraped_state, "p1_test_breaker") == "2.0"

        scraped_changes = _scrape("circuit_state_changes_total")
        assert 'to_state="open"' in scraped_changes

    asyncio.run(_drive())


def _gauge_value(scraped: str, name: str) -> str:
    """从抓取文本中取指定熔断器 Gauge 的样本值。"""
    for line in scraped.splitlines():
        if line.startswith("circuit_state{") and f'name="{name}"' in line:
            return line.rsplit(" ", 1)[1]
    raise AssertionError(f"gauge sample for {name} not found in scraped text")


def test_tracked_chat_openai_reports_prometheus_tokens(monkeypatch):
    """P1-5.1：编排域 LLM 调用要把 token 用量写进 llm_tokens_total。"""
    from agentix.application.agents import llm_factory as factory

    recorded: list[tuple] = []
    monkeypatch.setattr(
        "agentix.observability.metrics.record_llm_tokens",
        lambda *, model, direction, value: recorded.append((model, direction, value)),
    )

    # 桩掉父类 ainvoke（不打网络），返回带 usage_metadata 的伪响应
    async def _fake_super_ainvoke(self, *args, **kwargs):
        return types.SimpleNamespace(
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        )

    monkeypatch.setattr(factory.ChatOpenAI, "ainvoke", _fake_super_ainvoke)

    llm = factory._TrackedChatOpenAI(model="gpt-test", api_key="sk-test", role_tag="general")
    asyncio.run(llm.ainvoke("hi"))

    assert ("gpt-test", "in", 10) in recorded
    assert ("gpt-test", "out", 5) in recorded


def test_tracked_chat_openai_token_failure_is_non_fatal(monkeypatch):
    """指标上报抛异常不得影响主链路返回值（P0-4 风险缓解纪律）。"""

    def _boom(**kwargs):
        raise RuntimeError("metrics sink down")

    monkeypatch.setattr("agentix.observability.metrics.record_llm_tokens", _boom)

    async def _fake_super_ainvoke(self, *args, **kwargs):
        return types.SimpleNamespace(
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        )

    from agentix.application.agents import llm_factory as factory

    monkeypatch.setattr(factory.ChatOpenAI, "ainvoke", _fake_super_ainvoke)

    llm = factory._TrackedChatOpenAI(model="gpt-test", api_key="sk-test", role_tag="general")
    result = asyncio.run(llm.ainvoke("hi"))

    assert result.usage_metadata["total_tokens"] == 2
