"""R3-3：AgentRuntime 接入编排主链路（ChatService.ask）测试。

依据：《生产级 Agent 系统架构与实现设计》§95（请求执行流程）/§100（Phase 1）。
验收：成功/降级/超时路径行为完全不变（Strangler Phase 1），同时
metadata["runtime"] 携带类型化运行轨迹（AgentState 快照）。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage

from agentix.application.services.chat_service import ChatService
from agentix.config.settings import settings as settings_instance


def _run(coro):
    return asyncio.run(coro)


def _patch_obs(monkeypatch):
    import agentix.application.services.observability as obs_module

    svc = AsyncMock()
    monkeypatch.setattr(obs_module, "get_observability", lambda: svc)
    return svc


def _ok_result():
    return {
        "messages": [AIMessage(content="答复内容")],
        "router": {"type": "kb-query", "logic": "test"},
        "sources": [],
    }


class _FakeGraph:
    def __init__(self, delay=0.0, result=None, exc=None):
        self.delay = delay
        self.result = result if result is not None else _ok_result()
        self.exc = exc

    async def ainvoke(self, input_state, config=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return self.result


def test_ask_success_carries_runtime_trajectory(monkeypatch):
    """成功路径：metadata["runtime"] 含 completed 状态与 agent_graph 步骤。"""
    _patch_obs(monkeypatch)
    service = ChatService(graph_getter=lambda: _FakeGraph())

    out = _run(service.ask("试用期最长多久？", "s1"))

    assert out["status"] == "ok"
    runtime = out["metadata"]["runtime"]
    assert runtime["run_id"]
    assert runtime["status"] == "completed"
    assert runtime["task"]["kind"] == "chat"
    assert runtime["task"]["question"] == "试用期最长多久？"
    assert runtime["task"]["route_type"] == "kb-query"
    steps = runtime["steps"]
    assert len(steps) == 1
    assert steps[0]["step_type"] == "executing"
    assert steps[0]["name"] == "agent_graph"
    assert steps[0]["status"] == "succeeded"
    assert steps[0]["latency_ms"] is not None


def test_ask_degraded_error_carries_failed_trajectory(monkeypatch):
    """AgentError 可降级路径：runtime 轨迹落 FAILED + 错误码入轨迹。"""
    from agentix.domain.errors import AgentError, ErrorCode

    _patch_obs(monkeypatch)
    # GRAPH_UNAVAILABLE 属 DEGRADABLE（非 HTTP_FATAL）→ ask 返回降级 200
    code = ErrorCode.GRAPH_UNAVAILABLE
    service = ChatService(graph_getter=lambda: _FakeGraph(exc=AgentError(code, "kb down")))

    out = _run(service.ask("q", "s1"))

    assert out["status"] == "degraded"
    runtime = out["metadata"]["runtime"]
    assert runtime["status"] == "failed"
    assert runtime["error"]["code"] == code.value
    steps = runtime["steps"]
    assert steps and steps[0]["status"] == "failed"


def test_ask_timeout_behavior_unchanged(monkeypatch):
    """超时路径行为不变：仍映射 504 + AgentError 上下文（Runtime 不干预错误链）。"""
    from fastapi import HTTPException

    from agentix.domain.errors import AgentError, ErrorCode

    _patch_obs(monkeypatch)
    monkeypatch.setattr(settings_instance, "AGENT_TIMEOUT", 0.05)
    service = ChatService(graph_getter=lambda: _FakeGraph(delay=0.3))

    with pytest.raises(HTTPException) as ei:
        _run(service.ask("hi", "s1"))

    assert ei.value.status_code == 504
    assert ei.value.detail["error_code"] == ErrorCode.AGENT_TIMEOUT.value
    assert isinstance(ei.value.__context__, AgentError)


def test_ask_generic_failure_behavior_unchanged(monkeypatch):
    """未知异常路径：非重试类异常仍按 500 冒泡（Runtime 只落账不干预）。"""
    from fastapi import HTTPException

    _patch_obs(monkeypatch)
    service = ChatService(graph_getter=lambda: _FakeGraph(exc=RuntimeError("boom")))

    with pytest.raises(HTTPException) as ei:
        _run(service.ask("q", "s1"))

    assert ei.value.status_code == 500
    assert "internal_error" in str(ei.value.detail)


def test_runtime_zero_budget_enforces_nothing(monkeypatch):
    """观测模式：即使图里发生大量逻辑步数/调用，Runtime 不做任何限额拦截。"""
    _patch_obs(monkeypatch)
    service = ChatService(graph_getter=lambda: _FakeGraph())
    out = _run(service.ask("q", "s1"))
    budget = out["metadata"]["runtime"]["budget"]
    assert budget["max_steps"] == 0
    assert budget["max_llm_calls"] == 0
    assert budget["max_tool_calls"] == 0
    assert budget["max_tokens"] == 0
    assert budget["max_cost_usd"] == 0.0
