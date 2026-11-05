"""LG-2-6：图执行 `durability` 参数化（原生三档 sync / async / exit）。

覆盖面与验收口径（指导文档 §4.3 LG-2-6）：
- **默认 async 与基线逐位等价**：默认档下 ``durability_kwargs()`` 返回空 dict，
  四处图执行调用点不改变既有调用形状（框架在 ``durability=None`` 时按同一默认
  值解析）；「默认值」只取自 Settings 字段自己的默认，不另写字面量；
- **显式 sync 真的等落盘**：慢速 saver（每次写延迟 50ms）下，恢复链在第 3 个
  节点能读到本轮前两步的 checkpoint；async 档下读不到（同一条用例两种读数）；
- **exit 只在退出时写**：同一条两节点图，默认档历史 4 条、``exit`` 只 1 条；
  ``astream_events`` 经 ``**kwargs`` 下传同样生效（流式路径依赖这一点）；
- **非法取值启动即报错**：``GRAPH_DURABILITY`` 是 ``Literal``，Settings 实例化
  抛 ``ValidationError``，不许静默回落；
- **四处调用点透传**：chat 的 ask（``ainvoke``）与流式（``astream_events``）、
  ``lg_builder.resume_in_loop``、``breakpoint_resume.resume_thread``；
- **静态守卫**：四处调用点不得内联 ``durability=`` 关键字字面量（约定 6 ④）。
"""

from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path
from typing import Any, Dict, TypedDict

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from agentix.application.services.breakpoint_resume import CheckpointRecovery
from agentix.application.services.chat_service import ChatService
from agentix.config import settings
from agentix.config.settings import Settings
from agentix.orchestration.langgraph import lg_builder
from agentix.orchestration.langgraph.graph_config import durability_kwargs

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run(coro):
    return asyncio.run(coro)


def _collect(agen):
    async def _run_all():
        return [event async for event in agen]

    return asyncio.run(_run_all())


def _patch_obs(monkeypatch):
    import agentix.application.services.observability as obs_module
    from unittest.mock import AsyncMock

    svc = AsyncMock()
    monkeypatch.setattr(obs_module, "get_observability", lambda: svc)
    return svc


def _ok_result():
    return {"messages": [AIMessage(content="答复内容")], "router": {"type": "kb-query"}, "sources": []}


class _CapturingGraph:
    def __init__(self):
        self.captured: Dict[str, Any] = {}

    async def ainvoke(self, input_state, config=None, **kwargs):
        self.captured = {"config": config, "kwargs": kwargs}
        return _ok_result()


class _StreamGraph:
    def __init__(self):
        self.captured: Dict[str, Any] = {}

    async def astream_events(self, input_state, config=None, version=None, **kwargs):
        self.captured = {"config": config, "version": version, "kwargs": kwargs}
        yield {"event": "on_chain_end", "name": "LangGraph", "data": {}, "metadata": {}}


class _FakeRedis:
    def __init__(self):
        self.store: Dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


class _ResumeGraphStub:
    def __init__(self):
        self.kwargs: list = []

    def get_state(self, config):
        return type("S", (), {"values": {"messages": []}})()

    async def ainvoke(self, input, config=None, **kwargs):
        self.kwargs.append(kwargs)
        return {"messages": []}


# ---------------------------------------------------------------------------
# 1. 配置面：默认 async、默认值单一来源、非法取值启动即报错
# ---------------------------------------------------------------------------


def test_default_durability_is_async():
    assert Settings.model_fields["GRAPH_DURABILITY"].default == "async"
    assert settings.GRAPH_DURABILITY == "async"


@pytest.mark.parametrize("value", ["bogus", "SYNC", "", "none"])
def test_invalid_durability_fails_at_settings_instantiation(value):
    with pytest.raises(ValidationError) as ei:
        Settings(_env_file=None, SECRET_KEY="x", DEBUG=True, GRAPH_DURABILITY=value)
    assert "GRAPH_DURABILITY" in str(ei.value)


def test_durability_kwargs_empty_at_default():
    # 默认档不传参数：既有调用形状不变（与基线逐位等价）
    assert durability_kwargs() == {}


def test_durability_kwargs_passthrough_non_default(monkeypatch):
    for value in ("sync", "exit"):
        monkeypatch.setattr(settings, "GRAPH_DURABILITY", value)
        assert durability_kwargs() == {"durability": value}


# ---------------------------------------------------------------------------
# 2. 四处调用点透传
# ---------------------------------------------------------------------------


def test_chat_service_ask_forwards_durability(monkeypatch):
    _patch_obs(monkeypatch)
    monkeypatch.setattr(settings, "GRAPH_DURABILITY", "sync")
    graph = _CapturingGraph()

    _run(ChatService(graph_getter=lambda: graph).ask("hi", "s1"))

    assert graph.captured["kwargs"] == {"durability": "sync"}


def test_chat_service_stream_forwards_durability(monkeypatch):
    monkeypatch.setattr(settings, "GRAPH_DURABILITY", "exit")
    graph = _StreamGraph()

    _collect(ChatService(graph_getter=lambda: graph).stream_events("hi", "s1"))

    assert graph.captured["kwargs"] == {"durability": "exit"}


def test_resume_in_loop_forwards_durability(monkeypatch):
    monkeypatch.setattr(settings, "GRAPH_DURABILITY", "sync")
    captured: list = []

    class _GraphStub:
        async def ainvoke(self, input, config=None, **kwargs):
            captured.append(kwargs)
            return {}

    monkeypatch.setattr(lg_builder, "graph", _GraphStub())
    _run(lg_builder.resume_in_loop("t-1", "approve"))

    assert captured[-1] == {"durability": "sync"}


def test_resume_thread_forwards_durability(monkeypatch):
    monkeypatch.setattr(settings, "GRAPH_DURABILITY", "sync")
    graph = _ResumeGraphStub()

    _run(CheckpointRecovery(redis_client=_FakeRedis()).resume_thread("t-2", graph))

    assert graph.kwargs[-1] == {"durability": "sync"}


def test_default_durability_sends_no_kwarg(monkeypatch):
    _patch_obs(monkeypatch)
    graph = _CapturingGraph()

    _run(ChatService(graph_getter=lambda: graph).ask("hi", "s1"))

    assert graph.captured["kwargs"] == {}


# ---------------------------------------------------------------------------
# 3. 行为：sync 等落盘 / async 不等 / exit 只写一次
# ---------------------------------------------------------------------------


class _SlowSaver(MemorySaver):
    """每次写延迟 50ms，让 async 档的后台写在下游节点执行时必然未完成。"""

    async def aput(self, config, checkpoint, metadata, new_versions):
        await asyncio.sleep(0.05)
        return await super().aput(config, checkpoint, metadata, new_versions)

    def put(self, config, checkpoint, metadata, new_versions):
        time.sleep(0.05)
        return super().put(config, checkpoint, metadata, new_versions)


def _build_resume_graph(saver: _SlowSaver, seen: Dict[str, int], thread_id: str):
    """gate（interrupt）→ s1 → s2；s2 记录此刻存储里已落的 checkpoint 条数。"""

    def gate(state: Dict[str, Any]):
        interrupt({"reason": "need approval"})
        return {"n": state.get("n", 0) + 1}

    def s1(state: Dict[str, Any]):
        return {"n": state["n"] + 1}

    def s2(state: Dict[str, Any]):
        seen["at_s2"] = sum(len(c) for c in saver.storage.get(thread_id, {}).values())
        return {"n": state["n"] + 1}

    builder = StateGraph(dict)
    builder.add_node("gate", gate)
    builder.add_node("s1", s1)
    builder.add_node("s2", s2)
    builder.add_edge(START, "gate")
    builder.add_edge("gate", "s1")
    builder.add_edge("s1", "s2")
    builder.add_edge("s2", END)
    return builder.compile(checkpointer=saver)


def _seed_and_resume(monkeypatch, durability: str) -> tuple:
    saver = _SlowSaver()
    thread_id = f"t-dur-{durability}"
    seen: Dict[str, int] = {}
    graph = _build_resume_graph(saver, seen, thread_id)
    cfg = {"configurable": {"thread_id": thread_id}}
    _run(graph.ainvoke({"n": 0}, cfg))
    baseline = sum(len(c) for c in saver.storage.get(thread_id, {}).values())

    monkeypatch.setattr(settings, "GRAPH_DURABILITY", durability)
    monkeypatch.setattr(lg_builder, "graph", graph)
    _run(lg_builder.resume_in_loop(thread_id, "approve"))
    return baseline, seen["at_s2"]


def test_sync_durability_persists_before_next_step(monkeypatch):
    baseline, at_s2 = _seed_and_resume(monkeypatch, "sync")
    # gate 与 s1 两步都已落盘后才轮到 s2（1.2.18 实测 baseline+2）
    assert at_s2 >= baseline + 2


def test_async_durability_does_not_wait(monkeypatch):
    baseline, at_s2 = _seed_and_resume(monkeypatch, "async")
    # 每步后台写、下一步不等：s2 执行时本轮两步的写入都还没落
    assert at_s2 == baseline


class _TwoStep(TypedDict, total=False):
    n: int


def _two_step_graph():
    def a(state: _TwoStep):
        return {"n": state.get("n", 0) + 1}

    def b(state: _TwoStep):
        return {"n": state["n"] + 1}

    builder = StateGraph(_TwoStep)
    builder.add_node("a", a)
    builder.add_node("b", b)
    builder.add_edge(START, "a")
    builder.add_edge("a", "b")
    builder.add_edge("b", END)
    return builder.compile(checkpointer=MemorySaver())


def test_astream_events_forwards_durability_natively():
    """流式路径用的 ``astream_events`` 经 ``**kwargs`` 下传 durability（实测口径）。

    默认 async ⇒ 4 条历史；``exit`` ⇒ 只在退出时写 1 条。流式调用点把
    ``durability=`` 传给它而不是 ``astream``，这条用例钉住该传法真的生效。
    """

    async def run_one(durability: str) -> int:
        graph = _two_step_graph()
        thread_id = f"t-{durability}"
        cfg = {"configurable": {"thread_id": thread_id}}
        async for _ in graph.astream_events(
            {"n": 0}, config=cfg, version="v2", durability=durability
        ):
            pass
        return len([s async for s in graph.aget_state_history(cfg)])

    assert asyncio.run(run_one("async")) == 4
    assert asyncio.run(run_one("exit")) == 1


# ---------------------------------------------------------------------------
# 4. 静态守卫：调用点不得内联 durability 字面量
# ---------------------------------------------------------------------------


def _durability_keyword_calls(path: Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and any(kw.arg == "durability" for kw in node.keywords)
    ]


@pytest.mark.parametrize(
    "relpath",
    [
        "agentix/application/services/chat_service.py",
        "agentix/application/services/breakpoint_resume.py",
        "agentix/orchestration/langgraph/lg_builder.py",
    ],
)
def test_call_sites_use_helper_not_literal(relpath):
    assert _durability_keyword_calls(PROJECT_ROOT / relpath) == [], (
        f"{relpath} 内联了 durability= 字面量；应改经 graph_config.durability_kwargs()"
    )
