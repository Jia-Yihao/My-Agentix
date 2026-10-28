"""LG-3-6：优雅关闭（langgraph 1.2 原生 ``RunControl`` → ``GraphDrained``）。

覆盖面与验收口径（指导文档 §4.3 LG-3-6）：
- **① 开关默认关**：``RUN_CONTROL_ENABLED`` 默认 False；关时 ``_startup`` 不装
  信号处理器、``astream_events`` 不传 ``control`` 形参（调用形状与基线逐位等价）、
  协调器登记簿保持空；
- **② 开时 SIGTERM 可观察**：信号 → 协调器 → 运行中的图在**超步边界**抛
  ``GraphDrained``，且该线程可被 ``alist()`` 列出（与 LG-2-1 启动恢复的衔接点）；
- **③ drain 有上限**：``wait_for_drain`` 到点返回 False（不无限等）；信号处理
  函数的强制退出计时器到点真的调 ``os._exit``；
- **④ 每请求新建**：``DrainCoordinator.begin()`` 每次返回**不同**的 ``RunControl``
  （复用已 drain 的实例 = 永久 drain）；两次流式请求拿到两个不同控制面，且
  运行结束后登记簿清空。

另外钉住三件容易做错的事：
- 信号处理器**串接**原处理器（不串接 = uvicorn 再也收不到 SIGTERM）；
- 关停期间新运行拿到**预置位**控制面（第一超步即退出，不接新任务）；
- ``GraphDrained`` 在流式外层被单独捕获：按 ``cancelled`` 收口、如实告知
  「已保存」、仍然发唯一 ``done``——不许落进通用 ``except Exception`` 的
  「处理请求时出错」。
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from typing import Any, AsyncGenerator, Dict, TypedDict

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphDrained
from langgraph.graph import END, StateGraph
from langgraph.runtime import RunControl

from agentix.application.services.chat_service import ChatService
from agentix.config import settings
from agentix.config.settings import Settings
from agentix.orchestration.langgraph import policies
from agentix.orchestration.langgraph.policies import DrainCoordinator


def _run(coro):
    return asyncio.run(coro)


def _collect(agen):
    async def _run_all():
        return [event async for event in agen]

    return asyncio.run(_run_all())


def _fresh_coordinator(monkeypatch) -> DrainCoordinator:
    """把进程级单例换成用例私有实例（``signal.signal`` 是进程级的，必须隔离）。"""
    coordinator = DrainCoordinator()
    monkeypatch.setattr(policies, "_coordinator", coordinator)
    return coordinator


# ---------------------------------------------------------------------------
# 真图（带 checkpointer）：drain → GraphDrained → alist 可见
# ---------------------------------------------------------------------------


class _S(TypedDict):
    n: int
    trail: list


async def _step_a(state: _S) -> dict:
    await asyncio.sleep(0.02)
    return {"n": state["n"] + 1, "trail": state.get("trail", []) + ["a"]}


async def _step_b(state: _S) -> dict:
    await asyncio.sleep(0.02)
    return {"n": state["n"] + 1, "trail": state.get("trail", []) + ["b"]}


async def _step_c(state: _S) -> dict:
    await asyncio.sleep(0.02)
    return {"n": state["n"] + 1, "trail": state.get("trail", []) + ["c"]}


def _three_step_graph():
    g = StateGraph(_S)
    g.add_node("a", _step_a)
    g.add_node("b", _step_b)
    g.add_node("c", _step_c)
    g.set_entry_point("a")
    g.add_edge("a", "b")
    g.add_edge("b", "c")
    g.add_edge("c", END)
    return g.compile(checkpointer=InMemorySaver())


def _thread_cfg(thread_id: str) -> Dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


# ---------------------------------------------------------------------------
# 1. 配置面与导出
# ---------------------------------------------------------------------------


class TestSettingsAndExports:
    def test_flag_defaults_off(self):
        assert Settings.model_fields["RUN_CONTROL_ENABLED"].default is False
        assert settings.RUN_CONTROL_ENABLED is False

    def test_drain_timeout_default_and_lower_bound(self):
        assert Settings.model_fields["RUN_DRAIN_TIMEOUT_S"].default == 30.0
        with pytest.raises(Exception) as ei:
            Settings(_env_file=None, SECRET_KEY="x", DEBUG=True, RUN_DRAIN_TIMEOUT_S=0)
        assert "RUN_DRAIN_TIMEOUT_S" in str(ei.value)

    def test_exports(self):
        assert "DrainCoordinator" in policies.__all__
        assert "get_drain_coordinator" in policies.__all__
        assert "GraphDrained" in policies.__all__
        assert policies.GraphDrained is GraphDrained


# ---------------------------------------------------------------------------
# 2. 协调器：每运行新建 / 注销 / 关停期间预置位
# ---------------------------------------------------------------------------


class TestCoordinator:
    def test_begin_creates_a_fresh_control_per_run(self):
        coordinator = DrainCoordinator()
        k1, first = coordinator.begin()
        k2, second = coordinator.begin()
        assert isinstance(first, RunControl) and isinstance(second, RunControl)
        assert first is not second, "复用同一个控制面 = 一次 drain 永久关掉后续所有运行"
        assert coordinator.active_count() == 2
        coordinator.end(k1)
        coordinator.end(k2)
        assert coordinator.active_count() == 0

    def test_end_is_idempotent(self):
        coordinator = DrainCoordinator()
        key, _ = coordinator.begin()
        coordinator.end(key)
        coordinator.end(key)  # 正常结束 + finally 各调一次也不许炸
        assert coordinator.active_count() == 0

    def test_request_drain_flags_every_active_control(self):
        coordinator = DrainCoordinator()
        _, first = coordinator.begin()
        _, second = coordinator.begin()
        assert coordinator.request_drain("signal:15") == 2
        assert coordinator.drain_requested is True
        assert first.drain_requested is True
        assert second.drain_requested is True
        assert first.drain_reason == "signal:15"

    def test_new_control_after_drain_is_pre_drained(self):
        coordinator = DrainCoordinator()
        coordinator.request_drain("shutdown")
        _, control = coordinator.begin()
        assert control.drain_requested is True, "关停期间新运行不许接新超步"

    def test_wait_for_drain_returns_true_when_idle(self):
        coordinator = DrainCoordinator()
        assert _run(coordinator.wait_for_drain(0.5)) is True

    def test_wait_for_drain_is_bounded(self, monkeypatch):
        coordinator = DrainCoordinator()
        coordinator.begin()  # 永远不注销

        async def _scenario():
            started = time.monotonic()
            drained = await coordinator.wait_for_drain(0.1)
            return drained, time.monotonic() - started

        drained, elapsed = _run(_scenario())
        assert drained is False
        assert elapsed < 2.0, f"wait_for_drain 没有上限：等了 {elapsed:.2f}s"

    def test_wait_for_drain_returns_true_after_end(self):
        coordinator = DrainCoordinator()
        key, _ = coordinator.begin()

        async def _scenario():
            async def _finish_soon():
                await asyncio.sleep(0.05)
                coordinator.end(key)

            task = asyncio.create_task(_finish_soon())
            drained = await coordinator.wait_for_drain(2.0)
            await task
            return drained

        assert _run(_scenario()) is True


# ---------------------------------------------------------------------------
# 3. 真图：drain 在超步边界停摆，checkpoint 留下且 alist 可见（验收 ②）
# ---------------------------------------------------------------------------


class TestRealGraphDrain:
    def test_drain_raises_graph_drained_and_thread_stays_listable(self):
        graph = _three_step_graph()
        coordinator = DrainCoordinator()
        _, control = coordinator.begin()
        cfg = _thread_cfg("lg36-drain-1")

        async def _scenario():
            phases = []
            drained = None
            try:
                async for event in graph.astream_events(
                    {"n": 0, "trail": []}, config=cfg, version="v2", control=control
                ):
                    if (
                        event.get("event") == "on_chain_start"
                        and event.get("name") == "b"
                    ):
                        coordinator.request_drain("signal:15")
                phases.append("finished-without-drain")
            except GraphDrained as exc:
                drained = exc
            rows = [row async for row in graph.checkpointer.alist(cfg)]
            state = await graph.aget_state(cfg)
            return drained, rows, state, phases

        drained, rows, state, phases = _run(_scenario())
        assert isinstance(drained, GraphDrained), f"未观察到 GraphDrained：{phases}"
        assert drained.reason == "signal:15"
        assert rows, "drain 后线程必须可被 alist() 列出（恢复链的入口）"
        assert state.next, "drain 后必须留下可续跑的下一步"
        # 停摆发生在超步边界：b 跑完、c 还没开始
        assert state.values.get("trail") == ["a", "b"]

    def test_no_control_means_normal_completion(self):
        graph = _three_step_graph()

        async def _scenario():
            return [
                event
                async for event in graph.astream_events(
                    {"n": 0, "trail": []},
                    config=_thread_cfg("lg36-noctl"),
                    version="v2",
                )
            ]

        assert _run(_scenario())  # 不传 control = 基线路径，跑完不抛

    def test_reused_drained_control_never_runs_a_node(self):
        graph = _three_step_graph()
        control = RunControl()
        control.request_drain("already")

        async def _scenario():
            with pytest.raises(GraphDrained):
                async for _ in graph.astream_events(
                    {"n": 0, "trail": []},
                    config=_thread_cfg("lg36-reuse"),
                    version="v2",
                    control=control,
                ):
                    pass
            return await graph.aget_state(_thread_cfg("lg36-reuse"))

        state = _run(_scenario())
        assert state.values.get("trail", []) == []


# ---------------------------------------------------------------------------
# 4. 信号处理：串接原处理器 + 上限计时器（验收 ②/③）
# ---------------------------------------------------------------------------


class TestSignalHandling:
    def test_signal_handler_drains_then_forwards(self, monkeypatch):
        from agentix import main

        coordinator = _fresh_coordinator(monkeypatch)
        _, control = coordinator.begin()
        armed: list = []
        monkeypatch.setattr(main, "_arm_drain_deadline", lambda t: armed.append(t))
        forwarded: list = []

        handler = main._make_drain_signal_handler(
            coordinator, lambda signum, frame: forwarded.append((signum, frame))
        )
        handler(signal.SIGTERM, None)

        assert control.drain_requested is True
        assert coordinator.drain_requested is True
        assert forwarded == [(signal.SIGTERM, None)], "不串接原处理器 = uvicorn 收不到退出信号"
        assert armed == [settings.RUN_DRAIN_TIMEOUT_S]

    def test_install_and_uninstall_round_trip(self, monkeypatch):
        from agentix import main

        _fresh_coordinator(monkeypatch)
        monkeypatch.setattr(main, "_arm_drain_deadline", lambda t: None)
        monkeypatch.setattr(main, "_drain_prev_handlers", {})
        monkeypatch.setattr(settings, "RUN_CONTROL_ENABLED", True)
        before = signal.getsignal(signal.SIGTERM)
        try:
            assert main._install_drain_signal_handlers() is True
            installed = signal.getsignal(signal.SIGTERM)
            assert callable(installed) and installed is not before
            # 端到端：直接派发 OS 会派发的那个函数对象
            installed(signal.SIGTERM, None)
            assert policies.get_drain_coordinator().drain_requested is True
        finally:
            main._uninstall_drain_signal_handlers()
        assert signal.getsignal(signal.SIGTERM) is before

    def test_install_is_a_noop_when_flag_off(self, monkeypatch):
        from agentix import main

        _fresh_coordinator(monkeypatch)
        monkeypatch.setattr(main, "_drain_prev_handlers", {})
        before_term = signal.getsignal(signal.SIGTERM)
        before_int = signal.getsignal(signal.SIGINT)
        assert settings.RUN_CONTROL_ENABLED is False
        assert main._install_drain_signal_handlers() is False
        assert signal.getsignal(signal.SIGTERM) is before_term
        assert signal.getsignal(signal.SIGINT) is before_int
        assert main._drain_prev_handlers == {}

    def test_deadline_forces_exit(self, monkeypatch):
        from agentix import main

        exits: list = []
        monkeypatch.setattr(main, "_drain_deadline_timer", None)
        monkeypatch.setattr(os, "_exit", lambda code: exits.append(code))
        try:
            main._arm_drain_deadline(0.05)
            deadline = time.monotonic() + 2.0
            while not exits and time.monotonic() < deadline:
                time.sleep(0.01)
            assert exits == [1], "drain 超时没有强制退出 = 可能无限等"
        finally:
            main._cancel_drain_deadline()

    def test_deadline_is_armed_once_and_cancellable(self, monkeypatch):
        from agentix import main

        monkeypatch.setattr(main, "_drain_deadline_timer", None)
        fired: list = []
        monkeypatch.setattr(os, "_exit", lambda code: fired.append(code))
        try:
            main._arm_drain_deadline(30.0)
            timer = main._drain_deadline_timer
            main._arm_drain_deadline(30.0)  # 重复信号不许叠计时器
            assert main._drain_deadline_timer is timer
            main._cancel_drain_deadline()
            assert main._drain_deadline_timer is None
            assert fired == []
        finally:
            main._cancel_drain_deadline()

    def test_drain_helper_is_a_noop_when_flag_off(self, monkeypatch):
        from agentix import main

        coordinator = _fresh_coordinator(monkeypatch)
        coordinator.begin()
        _run(main._drain_in_flight_runs())
        assert coordinator.drain_requested is False
        assert coordinator.active_count() == 1

    def test_shutdown_drain_helper_is_bounded(self, monkeypatch):
        from agentix import main

        coordinator = _fresh_coordinator(monkeypatch)
        monkeypatch.setattr(settings, "RUN_CONTROL_ENABLED", True)
        coordinator.begin()  # 永不注销的运行
        cancelled: list = []
        monkeypatch.setattr(main, "_cancel_drain_deadline", lambda: cancelled.append(1))
        monkeypatch.setattr(settings, "RUN_DRAIN_TIMEOUT_S", 0.1)

        started = time.monotonic()
        _run(main._drain_in_flight_runs())
        elapsed = time.monotonic() - started
        assert elapsed < 2.0
        assert coordinator.drain_requested is True
        assert cancelled == [], "没排空就不许取消上限计时器"


# ---------------------------------------------------------------------------
# 5. chat_service 接线（验收 ①/④ + 流式外层捕获）
# ---------------------------------------------------------------------------


class _StreamGraph:
    """记录 ``astream_events`` 的 kwargs；正常吐一帧最外层图结束事件。"""

    def __init__(self) -> None:
        self.calls: list = []

    async def astream_events(
        self, input_state, config=None, version=None, **kwargs
    ) -> AsyncGenerator[Dict[str, Any], None]:
        self.calls.append({"version": version, "kwargs": kwargs})
        yield {"event": "on_chain_end", "name": "LangGraph", "data": {}, "metadata": {}}


class _DrainedGraph:
    def __init__(self) -> None:
        self.calls: list = []

    async def astream_events(
        self, input_state, config=None, version=None, **kwargs
    ) -> AsyncGenerator[Dict[str, Any], None]:
        self.calls.append({"version": version, "kwargs": kwargs})
        raise GraphDrained("signal:15")
        yield  # pragma: no cover - 只为让本函数成为异步生成器


class TestChatServiceWiring:
    def test_flag_off_sends_no_control_kwarg(self):
        graph = _StreamGraph()
        _collect(ChatService(graph_getter=lambda: graph).stream_events("hi", "s1"))
        assert graph.calls and "control" not in graph.calls[0]["kwargs"]

    def test_flag_off_leaves_coordinator_untouched(self, monkeypatch):
        coordinator = _fresh_coordinator(monkeypatch)
        graph = _StreamGraph()
        _collect(ChatService(graph_getter=lambda: graph).stream_events("hi", "s1"))
        assert coordinator.active_count() == 0

    def test_flag_on_creates_one_fresh_control_per_request(self, monkeypatch):
        coordinator = _fresh_coordinator(monkeypatch)
        monkeypatch.setattr(settings, "RUN_CONTROL_ENABLED", True)
        graph = _StreamGraph()
        service = ChatService(graph_getter=lambda: graph)

        async def _two_requests():
            out = []
            for _ in range(2):
                out.append([e async for e in service.stream_events("hi", "s1")])
            return out

        _run(_two_requests())
        controls = [call["kwargs"]["control"] for call in graph.calls]
        assert len(controls) == 2
        assert isinstance(controls[0], RunControl)
        assert controls[0] is not controls[1], "每请求必须新建 RunControl"
        assert coordinator.active_count() == 0, "运行结束必须注销，否则关停白等超时"

    def test_graph_drained_is_reported_as_restart_not_generic_error(self, monkeypatch):
        _fresh_coordinator(monkeypatch)
        monkeypatch.setattr(settings, "RUN_CONTROL_ENABLED", True)
        graph = _DrainedGraph()
        frames = _collect(ChatService(graph_getter=lambda: graph).stream_events("hi", "s1"))

        assert "control" in graph.calls[0]["kwargs"]
        errors = [f for f in frames if f.get("type") == "error"]
        assert errors and "已保存" in errors[0]["content"], frames
        assert "处理请求时出错" not in errors[0]["content"]
        assert [f for f in frames if f.get("type") == "done"], "drain 后必须仍有唯一 done"

    def test_registration_survives_until_stream_end(self, monkeypatch):
        coordinator = _fresh_coordinator(monkeypatch)
        monkeypatch.setattr(settings, "RUN_CONTROL_ENABLED", True)

        class _ObservingGraph:
            def __init__(self) -> None:
                self.seen_active = None

            async def astream_events(self, input_state, config=None, version=None, **kwargs):
                self.seen_active = coordinator.active_count()
                yield {
                    "event": "on_chain_end",
                    "name": "LangGraph",
                    "data": {"output": {"messages": [AIMessage(content="ok")]}},
                    "metadata": {},
                }

        graph = _ObservingGraph()
        _collect(ChatService(graph_getter=lambda: graph).stream_events("hi", "s1"))
        assert graph.seen_active == 1, "图执行期间必须在登记簿里（否则 SIGTERM 漏掉这次运行）"
        assert coordinator.active_count() == 0
