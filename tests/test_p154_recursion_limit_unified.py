"""LG-2-3：图调用 config 统一由 ``graph_config.thread_config`` 构造。

覆盖面与验收口径（指导文档 §4.3 LG-2-3）：
- **唯一构造点**：``thread_config()`` 产出
  ``{"configurable": {"thread_id": ...}, "recursion_limit": MAX_ITERATIONS}``，
  且 ``recursion_limit`` 必须是**顶层键**（塞进 ``configurable`` 图不识别）；
- **三处调用方实际生效**：``lg_builder.resume_in_loop`` /
  ``breakpoint_resume.resume_thread`` / ``chat_service`` 两条入口（ask /
  stream_events）都拿到 ``recursion_limit == settings.MAX_ITERATIONS``；用例把
  ``MAX_ITERATIONS`` 改成 7 再断言，证明读的是当次配置值（不是硬编码 10，
  更不是框架默认 25）；
- **行为差异消除**：恢复路径此前回落框架默认 25——统一后 ``MAX_ITERATIONS=3``
  时进环恢复真的在第 3 步撞 ``GraphRecursionError``（此前会多跑一倍多）；
- **静态守卫**：三个调用方都经 ``thread_config`` 构造，且不再出现把
  ``recursion_limit`` 当 dict 字面量键的写法（约定 6 ④：同一个判定不许两个源）。
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any, Dict, TypedDict
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import StateGraph
from langgraph.types import interrupt

from agentix.application.services.breakpoint_resume import CheckpointRecovery
from agentix.application.services.chat_service import ChatService
from agentix.config import settings
from agentix.orchestration.langgraph import lg_builder
from agentix.orchestration.langgraph.graph_config import thread_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 通用工具（与 test_p02_wiring 同风格：全离线、假 graph）
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _collect(agen):
    async def _run_all():
        return [event async for event in agen]

    return asyncio.run(_run_all())


def _patch_obs(monkeypatch):
    """把 observability.get_observability 换成 AsyncMock（避免连 Redis）。"""
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


class _Chunk:
    def __init__(self, content):
        self.content = content


def _ev(kind, name=None, data=None, metadata=None):
    return {"event": kind, "name": name, "data": data or {}, "metadata": metadata or {}}


class _CapturingGraph:
    """记录传给 ``ainvoke`` 的 config（ask 路径）。"""

    def __init__(self, result=None):
        self.result = result if result is not None else _ok_result()
        self.captured_config: Dict[str, Any] | None = None

    async def ainvoke(self, input_state, config=None):
        self.captured_config = config
        return self.result


class _StreamGraph:
    """回放预设事件序列；记录传给 ``astream_events`` 的 config。"""

    def __init__(self, events):
        self._events = events
        self.captured: Dict[str, Any] = {}

    async def astream_events(self, input_state, config=None, version=None):
        self.captured = {"config": config, "version": version}
        for event in self._events:
            yield event


class _FakeRedis:
    def __init__(self):
        self.store: Dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


class _ResumeGraphStub:
    """断点续跑替身：快照有值、无挂起中断；记录 ainvoke 收到的 config。"""

    def __init__(self):
        self.configs: list = []

    def get_state(self, config):
        return type("S", (), {"values": {"messages": []}})()

    async def ainvoke(self, input, config=None):
        self.configs.append(config)
        return {"messages": []}


# ---------------------------------------------------------------------------
# 1. 唯一构造点：形状 + 顶层键 + 现读配置
# ---------------------------------------------------------------------------


def test_thread_config_shape_and_top_level_key():
    cfg = thread_config("t-1", image_path="/tmp/a.png", incremental=True)
    assert cfg["configurable"] == {
        "thread_id": "t-1",
        "image_path": "/tmp/a.png",
        "incremental": True,
    }
    assert cfg["recursion_limit"] == settings.MAX_ITERATIONS
    # 必须是顶层键：塞进 configurable 图不识别（p02 同口径）
    assert "recursion_limit" not in cfg["configurable"]


def test_thread_config_reads_settings_at_call_time(monkeypatch):
    monkeypatch.setattr(settings, "MAX_ITERATIONS", 7)
    assert thread_config("t-1")["recursion_limit"] == 7
    monkeypatch.setattr(settings, "MAX_ITERATIONS", 3)
    assert thread_config("t-2")["recursion_limit"] == 3


# ---------------------------------------------------------------------------
# 2. 三处调用方：实际下发的 config 都带统一轮数预算
# ---------------------------------------------------------------------------


def test_resume_in_loop_uses_unified_config(monkeypatch):
    monkeypatch.setattr(settings, "MAX_ITERATIONS", 7)
    captured: list = []

    class _GraphStub:
        async def ainvoke(self, input, config=None):
            captured.append(config)
            return {}

    monkeypatch.setattr(lg_builder, "graph", _GraphStub())
    _run(lg_builder.resume_in_loop("t-1", "approve"))

    assert captured[-1]["configurable"] == {"thread_id": "t-1"}
    assert captured[-1]["recursion_limit"] == 7


def test_resume_thread_uses_unified_config(monkeypatch):
    monkeypatch.setattr(settings, "MAX_ITERATIONS", 7)
    redis = _FakeRedis()
    redis.store["checkpoint_recovery:t-2"] = json.dumps(
        {"thread_id": "t-2", "last_node": "execute_task"}
    )
    graph = _ResumeGraphStub()

    _run(CheckpointRecovery(redis_client=redis).resume_thread("t-2", graph))

    assert graph.configs[-1]["configurable"] == {"thread_id": "t-2"}
    assert graph.configs[-1]["recursion_limit"] == 7


def test_chat_service_ask_uses_unified_config(monkeypatch):
    _patch_obs(monkeypatch)
    monkeypatch.setattr(settings, "MAX_ITERATIONS", 7)
    graph = _CapturingGraph()

    _run(ChatService(graph_getter=lambda: graph).ask("hi", "s1"))

    cfg = graph.captured_config
    assert cfg["configurable"]["thread_id"] == "s1"
    assert cfg["recursion_limit"] == 7
    assert "recursion_limit" not in cfg["configurable"]


def test_chat_service_stream_uses_unified_config(monkeypatch):
    monkeypatch.setattr(settings, "MAX_ITERATIONS", 7)
    graph = _StreamGraph(events=[_ev("on_chain_end", name="LangGraph", metadata={})])

    _collect(ChatService(graph_getter=lambda: graph).stream_events("hi", "s1"))

    cfg = graph.captured["config"]
    assert cfg["configurable"]["thread_id"] == "s1"
    assert cfg["recursion_limit"] == 7
    assert "recursion_limit" not in cfg["configurable"]


# ---------------------------------------------------------------------------
# 3. 行为差异消除：恢复路径不再回落框架默认 25
# ---------------------------------------------------------------------------


class _LoopState(TypedDict):
    n: int


def test_recovery_resume_enforces_max_iterations(monkeypatch):
    """MAX_ITERATIONS=3 时，进环恢复在第 3 步撞 GraphRecursionError。

    统一前恢复 config 没有 recursion_limit ⇒ 落框架默认 25，同一段循环能多跑
    22 步才停；统一后与主链路同预算（这里证明不是 25）。
    """
    spins: list = []

    def gate(state: _LoopState) -> Dict[str, Any]:
        interrupt({"reason": "need approval"})
        return {"n": state["n"] + 1}

    def spin(state: _LoopState) -> Dict[str, Any]:
        spins.append(state["n"])
        return {"n": state["n"] + 1}

    builder = StateGraph(_LoopState)
    builder.add_node("gate", gate)
    builder.add_node("spin", spin)
    builder.set_entry_point("gate")
    builder.add_edge("gate", "spin")
    builder.add_edge("spin", "spin")
    graph = builder.compile(checkpointer=MemorySaver())

    thread = {"configurable": {"thread_id": "t-loop"}, "recursion_limit": 25}
    _run(graph.ainvoke({"n": 0}, thread))
    assert spins == []  # 首次调用停在 interrupt 处，spin 未执行

    monkeypatch.setattr(settings, "MAX_ITERATIONS", 3)
    monkeypatch.setattr(lg_builder, "graph", graph)

    with pytest.raises(GraphRecursionError) as ei:
        _run(lg_builder.resume_in_loop("t-loop", "approve"))

    assert "3" in str(ei.value)
    # 上限 3 ⇒ 恢复步 + 少量 spin（1.2.18 实测 4 次）；若回落默认 25 则这里不会抛错
    # （会跑满 24 次），所以「抛错 + 次数远小于 24」就是统一生效的证据。
    assert len(spins) <= 5


# ---------------------------------------------------------------------------
# 4. 静态守卫：三个调用方只经 thread_config 构造，不再内联 recursion_limit
# ---------------------------------------------------------------------------


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _dict_literal_recursion_keys(tree: ast.AST) -> list:
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key in node.keys:
            if isinstance(key, ast.Constant) and key.value == "recursion_limit":
                found.append(getattr(key, "lineno", -1))
    return found


@pytest.mark.parametrize(
    "relpath",
    [
        "agentix/orchestration/langgraph/lg_builder.py",
        "agentix/application/services/breakpoint_resume.py",
        "agentix/application/services/chat_service.py",
    ],
)
def test_callers_construct_config_only_via_helper(relpath):
    tree = ast.parse((PROJECT_ROOT / relpath).read_text(encoding="utf-8"))

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node) == "thread_config"
    ]
    assert calls, f"{relpath} 未经 graph_config.thread_config 构造 config"

    # 读 config["recursion_limit"] 不算违规；把该字符串当 dict 字面量键才是第二来源
    assert _dict_literal_recursion_keys(tree) == [], f"{relpath} 仍有内联 recursion_limit 字面量"
