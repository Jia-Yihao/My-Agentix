"""LG-2-2：`resume_thread` 的 409 判据——挂着未决中断的线程不许被 `ainvoke(None)` 续跑。

验收：

① 有挂起时端点返回 409，且**图状态不变**（checkpoint_id 不动、后续节点不执行）；
② 无挂起时行为与现状一致（照旧续跑并完成）；
③ 409 把调用方指向 `/ops/hitl/in-loop`，并报出待决中断数；
④ 启动恢复对这类线程跳过并继续（`main.py` 的 except 接线有 AST 守卫）。

两种「暂停」分开钉，这是本条最容易误伤的地方：节点内 `interrupt()` 会写
`snapshot.interrupts`（409 的对象）；`interrupt_before=[...]` 的静态暂停
`interrupts` 为空、只有 `next` 非空——它必须照旧能续跑（LG-2-1 的行为）。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Dict, List, TypedDict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agentix.application.services.breakpoint_resume import (
    CheckpointRecovery,
    ThreadInterruptedError,
)
from agentix.infrastructure.core.security import verify_api_key
from agentix.interfaces.http import ops_router
from agentix.orchestration.langgraph import lg_builder

_REPO = Path(__file__).resolve().parents[1]


class _State(TypedDict, total=False):
    n: int


class _FakeRedis:
    """元数据读写桩：`store` 为空即证明没写 failed/completed 元数据。"""

    def __init__(self) -> None:
        self.store: Dict[str, str] = {}

    async def get(self, key: str) -> Any:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: Any = None) -> None:
        self.store[key] = value


def _cfg(thread_id: str) -> Dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _build_graph(saver: MemorySaver, executed: List[str]):
    """a → approval（节点内 interrupt）→ b。`executed` 用来证明节点没被执行。"""

    async def node_a(state: _State) -> Dict[str, int]:
        executed.append("a")
        return {"n": state.get("n", 0) + 1}

    async def approval(state: _State) -> Dict[str, int]:
        executed.append("approval")
        decision = interrupt({"reason": "need approval"})
        return {"n": state["n"] + (100 if decision == "approve" else 0)}

    async def node_b(state: _State) -> Dict[str, int]:
        executed.append("b")
        return {"n": state["n"] + 10}

    builder = StateGraph(_State)
    builder.add_node("a", node_a)
    builder.add_node("approval", approval)
    builder.add_node("b", node_b)
    builder.add_edge(START, "a")
    builder.add_edge("a", "approval")
    builder.add_edge("approval", "b")
    builder.add_edge("b", END)
    return builder.compile(checkpointer=saver)


def _build_static_graph(saver: MemorySaver):
    """a → b，用 `interrupt_before` 暂停：`interrupts` 为空、`next` 非空。"""

    async def node_a(state: _State) -> Dict[str, int]:
        return {"n": state.get("n", 0) + 1}

    async def node_b(state: _State) -> Dict[str, int]:
        return {"n": state.get("n", 0) + 10}

    builder = StateGraph(_State)
    builder.add_node("a", node_a)
    builder.add_node("b", node_b)
    builder.add_edge(START, "a")
    builder.add_edge("a", "b")
    builder.add_edge("b", END)
    return builder.compile(checkpointer=saver)


def _make_client(monkeypatch, recovery: CheckpointRecovery, graph) -> TestClient:
    monkeypatch.setattr(ops_router, "get_checkpoint_recovery", lambda: recovery)
    monkeypatch.setattr(lg_builder, "graph", graph)
    app = FastAPI()
    app.include_router(ops_router.router)
    app.dependency_overrides[verify_api_key] = lambda: None
    return TestClient(app)


async def _interrupted_thread(saver: MemorySaver, executed: List[str], thread_id: str):
    app = _build_graph(saver, executed)
    await app.ainvoke({"n": 1}, _cfg(thread_id))
    snapshot = await app.aget_state(_cfg(thread_id))
    assert snapshot.interrupts, "前置条件：节点内 interrupt() 必须留下 snapshot.interrupts"
    return app, snapshot


@pytest.mark.asyncio
async def test_resume_thread_refuses_interrupted_thread_and_keeps_state():
    """验收①：抛 ThreadInterruptedError；checkpoint_id 不变、后续节点不执行、无 failed 元数据。"""
    saver = MemorySaver()
    executed: List[str] = []
    app, before = await _interrupted_thread(saver, executed, "waiting")

    redis = _FakeRedis()
    recovery = CheckpointRecovery(redis_client=redis, checkpointer=saver)

    with pytest.raises(ThreadInterruptedError) as excinfo:
        await recovery.resume_thread("waiting", app)

    assert excinfo.value.thread_id == "waiting"
    assert excinfo.value.pending == len(before.interrupts)

    after = await app.aget_state(_cfg("waiting"))
    assert (
        after.config["configurable"]["checkpoint_id"]
        == before.config["configurable"]["checkpoint_id"]
    ), "拒绝续跑不许产生新 checkpoint"
    assert "b" not in executed, "拒绝续跑不许执行后续节点"
    assert redis.store == {}, "治理层等待不写 failed/completed 元数据"


@pytest.mark.asyncio
async def test_endpoint_returns_409_pointing_to_hitl_in_loop(monkeypatch):
    """验收①③：端点 409，响应把调用方指向 /ops/hitl/in-loop 并报待决中断数。"""
    saver = MemorySaver()
    executed: List[str] = []
    app, before = await _interrupted_thread(saver, executed, "waiting")
    recovery = CheckpointRecovery(redis_client=_FakeRedis(), checkpointer=saver)

    with _make_client(monkeypatch, recovery, app) as client:
        response = client.post("/ops/threads/resume", json={"thread_id": "waiting"})

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "thread_interrupted"
    assert detail["resume_endpoint"] == "/ops/hitl/in-loop"
    assert detail["thread_id"] == "waiting"
    assert detail["pending_interrupts"] == len(before.interrupts)

    after = await app.aget_state(_cfg("waiting"))
    assert (
        after.config["configurable"]["checkpoint_id"]
        == before.config["configurable"]["checkpoint_id"]
    )
    assert "b" not in executed


@pytest.mark.asyncio
async def test_static_interrupt_before_thread_still_resumes(monkeypatch):
    """验收②：`interrupt_before` 的静态暂停 `interrupts` 为空 ⇒ 照旧续跑（不被 409 误伤）。"""
    saver = MemorySaver()
    app = _build_static_graph(saver)
    await app.ainvoke({"n": 1}, _cfg("static"), interrupt_before=["b"])

    snapshot = await app.aget_state(_cfg("static"))
    assert snapshot.next == ("b",) and not snapshot.interrupts

    recovery = CheckpointRecovery(redis_client=_FakeRedis(), checkpointer=saver)
    with _make_client(monkeypatch, recovery, app) as client:
        response = client.post("/ops/threads/resume", json={"thread_id": "static"})

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "resumed"
    done = await app.aget_state(_cfg("static"))
    assert done.next == ()


@pytest.mark.asyncio
async def test_endpoint_still_404_for_unknown_thread(monkeypatch):
    """无挂起且无 checkpoint 的线程仍是 404（LG-2-1 的判据不被 409 顶掉）。"""
    saver = MemorySaver()
    app = _build_graph(saver, [])
    recovery = CheckpointRecovery(redis_client=_FakeRedis(), checkpointer=saver)

    with _make_client(monkeypatch, recovery, app) as client:
        response = client.post("/ops/threads/resume", json={"thread_id": "ghost"})

    assert response.status_code == 404, response.text


def test_startup_recovery_catches_interrupted_thread():
    """验收④：启动恢复必须捕获 ThreadInterruptedError，否则一条待裁决线程会中断整轮。"""
    tree = ast.parse((_REPO / "agentix/main.py").read_text(encoding="utf-8"))
    caught = {
        handler.type.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Try)
        for handler in node.handlers
        if isinstance(handler.type, ast.Name)
    }
    assert "ThreadInterruptedError" in caught, (
        "main.py 的启动恢复没有捕 ThreadInterruptedError —— 请检查 _startup 的恢复循环"
    )
