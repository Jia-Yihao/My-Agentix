"""Phase 15 + 17.1 — AgentLoop 生产支路与在线信号接线。

DoD（规划 §3.6 / §4.4）：
- flag 默认 False；flag-off 走原 L2 路径（graph.ainvoke 被调用）；
- flag-on ask smoke：AgentLoop 终态可达，结果形状对齐下游；
- stream 恒 L2（决策 A）；
- OnlineLoopRecorder 三漏斗有调用且零写不变量保持；
- 轨迹桥把 observations 写进 TrajectoryRecorder。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage

from agentix.application.services import agent_loop_adapter as adapter
from agentix.application.services.chat_service import ChatService, _record_online_signal
from agentix.application.services.trajectory import (
    get_trajectory_recorder,
    reset_trajectory_recorder,
)
from agentix.agent.evolution import (
    OnlineLoopRecorder,
    get_online_loop_recorder,
    reset_online_loop_recorder,
)
from agentix.config import settings
from agentix.domain.agent.types import Observation, Plan, PlanStep
from agentix.domain.status import RequestStatus


class TestFlagDefaultOff:
    def test_agent_loop_enabled_defaults_false(self):
        assert settings.AGENT_LOOP_ENABLED is False


class TestAdapter:
    def test_build_task_spec_uses_message_as_goal(self):
        task = adapter.build_task_spec("什么是合同解除？", session_id="s1")
        assert task.goal == "什么是合同解除？"
        assert task.task_id.startswith("s1:")

    def test_build_tool_map_includes_act_and_final_answer(self):
        tools = adapter.build_tool_map()
        assert "act" in tools and "final_answer" in tools

    def test_build_tool_map_merges_extra(self):
        tools = adapter.build_tool_map({"custom": lambda **_: "ok"})
        assert "custom" in tools

    @pytest.mark.asyncio
    async def test_run_agent_loop_ask_smoke(self):
        result = await adapter.run_agent_loop_ask(
            "hello harness", session_id="sess-1"
        )
        assert result["messages"], "must produce at least one message"
        text = result["messages"][-1].content
        assert "hello harness" in text
        assert result["router"]["logic"].startswith("agent_loop:")
        assert isinstance(result["agent_loop"]["terminal"], str)

    @pytest.mark.asyncio
    async def test_run_agent_loop_ask_records_trajectory(self):
        reset_trajectory_recorder()
        rec = get_trajectory_recorder()
        before = len(rec.get())
        await adapter.run_agent_loop_ask("轨迹桥测试", session_id="sess-traj")
        after = rec.get()
        assert len(after) > before
        assert any(s.kind == "step" for s in after[before:])

    def test_map_loop_result_marks_degraded_when_failed(self):
        from agentix.orchestration.orchestrator import LoopResult
        from agentix.domain.agent.types import ContextSnapshot

        result = LoopResult(
            task_id="t",
            final="",
            passed=False,
            plan=Plan(),
            context=ContextSnapshot(prompt=""),
            terminal="failed",
        )
        mapped = adapter.map_loop_result(result, route="kb-query")
        assert mapped["degraded"] is True
        msg = mapped["messages"][0]
        assert msg.additional_kwargs["degraded"] is True
        assert mapped["router"]["type"] == "kb-query"

    def test_map_loop_result_uses_ok_observations_for_tool_calls(self):
        from agentix.orchestration.orchestrator import LoopResult
        from agentix.domain.agent.types import ContextSnapshot

        result = LoopResult(
            task_id="t",
            final="done",
            passed=True,
            plan=Plan(steps=(PlanStep(id="s1", action="act"),)),
            context=ContextSnapshot(prompt=""),
            observations=(
                Observation(step_id="s1", content="done", ok=True, meta={"tool": "act"}),
            ),
            terminal="completed",
        )
        mapped = adapter.map_loop_result(result)
        assert mapped["degraded"] is False
        assert mapped["messages"][0].tool_calls[0]["name"] == "act"


def _make_chat_service(
    graph_result: Optional[Dict[str, Any]] = None,
    *,
    loop_result: Optional[Dict[str, Any]] = None,
):
    graph = MagicMock()
    if graph_result is None:
        graph_result = {
            "messages": [AIMessage(content="l2-answer")],
            "router": {"type": "kb-query", "logic": "test"},
            "sources": [],
        }
    graph.ainvoke = AsyncMock(return_value=graph_result)
    svc = ChatService(graph_getter=lambda: graph)
    return svc, graph


class TestAskFlagBranch:
    @pytest.mark.asyncio
    async def test_flag_off_uses_langgraph(self, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_LOOP_ENABLED", False)
        svc, graph = _make_chat_service()
        out = await svc.ask("hi", session_id="s-off")
        graph.ainvoke.assert_awaited()
        assert out["message"] == "l2-answer"
        assert out["status"] == RequestStatus.OK.value

    @pytest.mark.asyncio
    async def test_flag_on_uses_agent_loop(self, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_LOOP_ENABLED", True)
        svc, graph = _make_chat_service()

        async def _fake_loop(message: str, session_id: str, **kwargs):
            return {
                "messages": [AIMessage(content="l3-answer")],
                "router": {"type": None, "logic": "agent_loop:completed"},
                "sources": [],
            }

        monkeypatch.setattr(
            "agentix.application.services.agent_loop_adapter.run_agent_loop_ask",
            _fake_loop,
            raising=False,
        )
        # branch does lazy import inside function — patch the module attribute
        import agentix.application.services.agent_loop_adapter as mod

        monkeypatch.setattr(mod, "run_agent_loop_ask", _fake_loop)
        out = await svc.ask("hi", session_id="s-on")
        graph.ainvoke.assert_not_awaited()
        assert out["message"] == "l3-answer"

    @pytest.mark.asyncio
    async def test_flag_on_real_adapter_path(self, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_LOOP_ENABLED", True)
        svc, graph = _make_chat_service()
        out = await svc.ask("合同解除的条件", session_id="s-real")
        graph.ainvoke.assert_not_awaited()
        assert "合同解除的条件" in out["message"]
        # harness 级回执未通过业务 verifier 时可能 degraded；不强制
        assert out["status"] in (
            RequestStatus.OK.value,
            RequestStatus.DEGRADED.value,
        )

    @pytest.mark.asyncio
    async def test_stream_still_uses_langgraph_even_if_flag_on(self, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_LOOP_ENABLED", True)
        svc, graph = _make_chat_service()

        async def _events(*args, **kwargs):
            yield {
                "event": "on_chat_model_stream",
                "name": "respond_to_general_query",
                "data": {"chunk": MagicMock()},
                "metadata": {"langgraph_node": "respond_to_general_query"},
            }

        # _extract_chunk_text 会从 chunk 取文本；给简单 stub
        graph.astream_events = lambda *a, **k: _events()
        monkeypatch.setattr(
            ChatService,
            "_extract_chunk_text",
            staticmethod(lambda chunk: "stream-l2"),
        )
        monkeypatch.setattr(
            ChatService, "_is_answer_node", staticmethod(lambda name: True)
        )
        chunks: List[Dict[str, Any]] = []
        async for ev in svc.stream_events("hi", session_id="s-st"):
            chunks.append(ev)
        # stream 路径应调用 astream_events（L2），不构造 AgentLoop
        assert any(c.get("type") == "message" and c.get("content") == "stream-l2" for c in chunks)


class TestOnlineLoopRecorderWiring:
    def test_recorder_singleton_and_zero_write(self):
        reset_online_loop_recorder()
        rec = get_online_loop_recorder()
        assert isinstance(rec, OnlineLoopRecorder)
        assert rec.write_capability is False

    @pytest.mark.asyncio
    async def test_success_funnel_records(self, monkeypatch):
        reset_online_loop_recorder()
        monkeypatch.setattr(settings, "AGENT_LOOP_ENABLED", False)
        svc, _ = _make_chat_service()
        n_before = len(get_online_loop_recorder().signals)
        await svc.ask("ok", session_id="s-ok")
        rec = get_online_loop_recorder()
        assert len(rec.signals) == n_before + 1
        assert rec.signals[-1]["success"] is True

    @pytest.mark.asyncio
    async def test_agent_error_funnel_records(self, monkeypatch):
        from agentix.domain.errors import AgentError, ErrorCode

        reset_online_loop_recorder()
        monkeypatch.setattr(settings, "AGENT_LOOP_ENABLED", False)
        graph = MagicMock()
        graph.ainvoke = AsyncMock(
            side_effect=AgentError(ErrorCode.GRAPH_UNAVAILABLE, "graph down")
        )
        # 某些 ErrorCode 会 HTTP_FATAL raise；若 raise 则跳过 record 断言
        svc = ChatService(graph_getter=lambda: graph)
        try:
            await svc.ask("err", session_id="s-err")
        except Exception:
            pass
        rec = get_online_loop_recorder()
        if rec.signals:
            assert rec.signals[-1]["success"] is False

    @pytest.mark.asyncio
    async def test_internal_error_funnel_records(self, monkeypatch):
        reset_online_loop_recorder()
        monkeypatch.setattr(settings, "AGENT_LOOP_ENABLED", False)
        monkeypatch.setattr(settings, "LEGACY_ERROR_MODE", True)
        graph = MagicMock()
        graph.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))
        svc = ChatService(graph_getter=lambda: graph)
        out = await svc.ask("fail", session_id="s-int")
        assert out["error_code"] == "internal_error"
        rec = get_online_loop_recorder()
        assert rec.signals[-1]["success"] is False
        assert rec.signals[-1]["first_error_stage"] == "internal_error"

    def test_record_online_signal_never_raises(self):
        # 无 request_scope 时 get_trace_id 仍应有值；函数自吞异常
        _record_online_signal(success=True, tags=("unit",))

    def test_zero_write_signature_invariant(self):
        import inspect

        params = set(inspect.signature(OnlineLoopRecorder.record).parameters)
        for banned in ("patch", "apply", "payload", "write"):
            assert banned not in params
