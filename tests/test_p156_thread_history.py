"""LG-2-5：历史回看端点（``GET /ops/threads/{tid}/history``，只读）。

覆盖面与验收口径（指导文档 §4.3 LG-2-5）：
- 返回的步骤序列与 checkpointer 原始历史（``get_state_history``）**条数与顺序
  逐条对齐**——数据源只有图自己的历史，不另造轨迹；
- 调用**不写任何状态**：调用前后 ``get_state`` 的 ``checkpoint_id`` 不变，
  且历史首条（最新）就是当前快照；
- 步骤摘要是投影（step / source / next / tasks / interrupts），不携带 values；
- ``limit`` 生效且有上下界（1–500，超界 422）；
- 未知线程 ⇒ 404；同步专用桩走 ``_state_history`` 的回退分支；
- 路由已注册。
"""

from __future__ import annotations

import asyncio
import operator
from types import SimpleNamespace
from typing import Annotated, Any, Dict, List, TypedDict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from agentix.application.services.breakpoint_resume import CheckpointRecovery
from agentix.infrastructure.core.security import verify_api_key
from agentix.interfaces.http import ops_router


class _S(TypedDict, total=False):
    n: int
    log: Annotated[list, operator.add]


def _build_graph():
    """两步串行图：input → a → b → END，共 4 条 checkpoint（含 input 那条）。"""

    def a(state: _S):
        return {"n": state.get("n", 0) + 1, "log": ["a"]}

    def b(state: _S):
        return {"n": state["n"] + 1, "log": ["b"]}

    builder = StateGraph(_S)
    builder.add_node("a", a)
    builder.add_node("b", b)
    builder.add_edge(START, "a")
    builder.add_edge("a", "b")
    builder.add_edge("b", END)
    return builder.compile(checkpointer=MemorySaver())


def _cfg(thread_id: str) -> Dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _run(graph, thread_id: str = "t-hist") -> None:
    asyncio.run(graph.ainvoke({"n": 0, "log": []}, _cfg(thread_id)))


def _raw_history(graph, thread_id: str = "t-hist") -> List[Any]:
    async def collect():
        return [s async for s in graph.aget_state_history(_cfg(thread_id))]

    return asyncio.run(collect())


class _FakeRedis:
    def __init__(self):
        self.store: Dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


def _recovery() -> CheckpointRecovery:
    return CheckpointRecovery(redis_client=_FakeRedis())


def _client(monkeypatch, graph) -> TestClient:
    import agentix.orchestration.langgraph.lg_builder as lg_builder

    monkeypatch.setattr(lg_builder, "graph", graph)
    monkeypatch.setattr(ops_router, "get_checkpoint_recovery", _recovery)

    app = FastAPI()
    app.include_router(ops_router.router)
    app.dependency_overrides[verify_api_key] = lambda: None
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. 服务层：与原始历史对齐 / 只读 / limit / 回退分支
# ---------------------------------------------------------------------------


def test_history_aligns_with_raw_checkpoint_history():
    graph = _build_graph()
    _run(graph)
    raw = _raw_history(graph)

    history = asyncio.run(_recovery().list_thread_history("t-hist", graph))

    assert len(history) == len(raw) == 4
    assert [h["checkpoint_id"] for h in history] == [
        s.config["configurable"]["checkpoint_id"] for s in raw
    ]
    # 新 → 旧：step 严格递减，input 那条是 -1
    assert [h["step"] for h in history] == [2, 1, 0, -1]
    assert history[-1]["source"] == "input"
    assert history[-1]["next"] == ["__start__"]
    assert history[0]["next"] == []


def test_history_is_read_only():
    graph = _build_graph()
    _run(graph)
    before = asyncio.run(graph.aget_state(_cfg("t-hist")))
    before_id = before.config["configurable"]["checkpoint_id"]

    history = asyncio.run(_recovery().list_thread_history("t-hist", graph))

    after = asyncio.run(graph.aget_state(_cfg("t-hist")))
    assert after.config["configurable"]["checkpoint_id"] == before_id
    # 历史首条就是当前快照：只读接口没有追加新步骤
    assert history[0]["checkpoint_id"] == before_id
    assert len(history) == len(_raw_history(graph))


def test_history_limit_returns_newest_slice():
    graph = _build_graph()
    _run(graph)

    history = asyncio.run(_recovery().list_thread_history("t-hist", graph, limit=2))

    assert [h["step"] for h in history] == [2, 1]


def test_history_summary_is_projection_without_values():
    graph = _build_graph()
    _run(graph)

    history = asyncio.run(_recovery().list_thread_history("t-hist", graph))

    assert set(history[0]) == {
        "checkpoint_id",
        "step",
        "source",
        "created_at",
        "next",
        "tasks",
        "interrupts",
    }
    assert history[1]["tasks"] == ["b"]
    assert all(h["interrupts"] == 0 for h in history)


def test_history_unknown_thread_is_none():
    graph = _build_graph()
    assert asyncio.run(_recovery().list_thread_history("nope", graph)) is None


def test_history_falls_back_to_sync_stub():
    """只有同步接口的桩：快照与历史都走回退分支。"""
    snapshot = SimpleNamespace(
        values={"n": 1},
        next=(),
        config={"configurable": {"checkpoint_id": "ck-1"}},
        metadata={"step": 0, "source": "loop"},
        created_at="2026-10-28T00:00:00+00:00",
        tasks=(),
        interrupts=(),
    )

    class _SyncOnlyGraph:
        def get_state(self, config):
            return snapshot

        def get_state_history(self, config, limit=None):
            return [snapshot]

    history = asyncio.run(
        _recovery().list_thread_history("t-sync", _SyncOnlyGraph(), limit=5)
    )

    assert history == [
        {
            "checkpoint_id": "ck-1",
            "step": 0,
            "source": "loop",
            "created_at": "2026-10-28T00:00:00+00:00",
            "next": [],
            "tasks": [],
            "interrupts": 0,
        }
    ]


# ---------------------------------------------------------------------------
# 2. 端点：200 / 404 / limit 上下界
# ---------------------------------------------------------------------------


def test_route_registered():
    paths = {getattr(r, "path", "") for r in ops_router.router.routes}
    assert "/ops/threads/{thread_id}/history" in paths


def test_endpoint_returns_history(monkeypatch):
    graph = _build_graph()
    _run(graph)
    current_id = asyncio.run(graph.aget_state(_cfg("t-hist"))).config["configurable"][
        "checkpoint_id"
    ]
    before_count = len(_raw_history(graph))

    with _client(monkeypatch, graph) as client:
        resp = client.get("/ops/threads/t-hist/history")
        assert resp.status_code == 200
        body = resp.json()
        assert body["thread_id"] == "t-hist"
        assert body["count"] == 4 == len(body["history"])
        assert body["history"][0]["checkpoint_id"] == current_id

        limited = client.get("/ops/threads/t-hist/history", params={"limit": 1})
        assert limited.status_code == 200
        assert limited.json()["count"] == 1

    # 端点全程只读：步骤数不变，当前 checkpoint 不变
    assert len(_raw_history(graph)) == before_count
    after_id = asyncio.run(graph.aget_state(_cfg("t-hist"))).config["configurable"][
        "checkpoint_id"
    ]
    assert after_id == current_id


def test_endpoint_unknown_thread_is_404(monkeypatch):
    graph = _build_graph()
    with _client(monkeypatch, graph) as client:
        resp = client.get("/ops/threads/nope/history")
    assert resp.status_code == 404


@pytest.mark.parametrize("limit", [0, 501])
def test_endpoint_rejects_out_of_range_limit(monkeypatch, limit):
    graph = _build_graph()
    _run(graph)
    with _client(monkeypatch, graph) as client:
        resp = client.get("/ops/threads/t-hist/history", params={"limit": limit})
    assert resp.status_code == 422
