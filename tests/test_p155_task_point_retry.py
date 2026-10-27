"""LG-2-4：按 task 的定点查看与重跑（``GET /ops/threads/{tid}/tasks`` /
``POST /ops/threads/{tid}/retry-task``）。

覆盖面与验收口径（指导文档 §4.3 LG-2-4）：
- 列表：task 只从图快照 ``snapshot.tasks`` 取（id / name / error / 中断数），
  线程不存在 ⇒ 404；
- 重跑：用计数桩证明**只重跑失败的 task**——同一超级步里已完成的兄弟 task
  不重放（引擎侧依据 checkpointer 的 ``pending_writes`` 台账）；
- 失败 task 重跑成功后不再出现在待执行列表；
- 已完成 task 经本端点重跑 ⇒ 409（堵住「重放成功 task」的路）；
- 未知线程 / 未知 task ⇒ 404；挂起中断的线程 ⇒ 409 并指向
  ``/ops/hitl/in-loop``；
- 路由已注册。

**与指导文档的偏差（有实测依据）**：文档建议用 ``update_state(task_id=...)``
实现「只重跑失败的 task」。实测 1.2.18 对失败超级步调
``aupdate_state(..., task_id=<失败 task>)`` 会把整步收敛成这一条 update：
兄弟 task 已落盘的结果连同 ``next`` 一起被丢弃（``done`` 从 ``["good"]`` 变空、
``next`` 变 ``()``），拿它做重试会损坏状态。原生 ``ainvoke(None)`` 本身只重跑
「没有已落盘写入」的 task，正好满足验收要求，故实现走原生 resume（见服务层
``retry_task`` docstring）。
"""

from __future__ import annotations

import asyncio
import operator
from typing import Annotated, Any, Dict, TypedDict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from agentix.application.services.breakpoint_resume import CheckpointRecovery
from agentix.infrastructure.core.security import verify_api_key
from agentix.interfaces.http import ops_router


class _FanState(TypedDict, total=False):
    items: list
    done: Annotated[list, operator.add]


class _GateState(TypedDict, total=False):
    stage: str


def _build_fanout_graph(executed: list, fail_items: set):
    """同一节点并行跑两个 task；``fail_items`` 里的 item 首次执行即失败。"""

    def dispatch(state: _FanState):
        return [Send("work", {"item": i}) for i in state["items"]]

    def work(payload: Dict[str, Any]):
        item = payload["item"]
        executed.append(item)
        if item in fail_items:
            fail_items.discard(item)  # 只失败一次，重跑能成功
            raise RuntimeError(f"boom:{item}")
        return {"done": [item]}

    builder = StateGraph(_FanState)
    builder.add_node("work", work)
    builder.add_conditional_edges(START, dispatch, ["work"])
    builder.add_edge("work", END)
    return builder.compile(checkpointer=MemorySaver())


def _build_interrupt_graph():
    def gate(state: _GateState):
        interrupt({"reason": "need approval"})
        return {"stage": "approved"}

    builder = StateGraph(_GateState)
    builder.add_node("gate", gate)
    builder.add_edge(START, "gate")
    builder.add_edge("gate", END)
    return builder.compile(checkpointer=MemorySaver())


def _cfg(thread_id: str) -> Dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _seed_fanout_failure(graph, thread_id: str = "t-fan") -> None:
    with pytest.raises(RuntimeError):
        asyncio.run(
            graph.ainvoke({"items": ["good", "bad"], "done": []}, _cfg(thread_id))
        )


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


def _failed_task_id(graph, thread_id: str = "t-fan") -> str:
    snapshot = asyncio.run(graph.aget_state(_cfg(thread_id)))
    for task in snapshot.tasks:
        if task.error is not None:
            return task.id
    raise AssertionError("no failed task on snapshot")


def _completed_task_id(graph, thread_id: str = "t-fan") -> str:
    snapshot = asyncio.run(graph.aget_state(_cfg(thread_id)))
    for task in snapshot.tasks:
        if task.error is None and task.result is not None:
            return task.id
    raise AssertionError("no completed sibling task on snapshot")


# ---------------------------------------------------------------------------
# 1. 服务层：列表 / 定点重跑 / 兄弟 task 不重放
# ---------------------------------------------------------------------------


def test_list_pending_tasks_projects_snapshot():
    executed: list = []
    graph = _build_fanout_graph(executed, {"bad"})
    _seed_fanout_failure(graph)

    tasks = asyncio.run(_recovery().list_pending_tasks("t-fan", graph))

    assert len(tasks) == 2
    assert {t["name"] for t in tasks} == {"work"}
    failed = [t for t in tasks if t["error"] is not None]
    done = [t for t in tasks if t["has_result"]]
    assert len(failed) == 1 and len(done) == 1
    assert "boom" in failed[0]["error"]
    assert tasks[0]["interrupts"] == 0
    # Send 扇出的 task path 以 __pregel_push 起步（1.2.18 实测），此处只钉非空
    assert tasks[0]["path"]


def test_list_pending_tasks_unknown_thread_is_none():
    graph = _build_fanout_graph([], set())
    assert asyncio.run(_recovery().list_pending_tasks("nope", graph)) is None


def test_retry_task_reruns_only_failed_task():
    executed: list = []
    graph = _build_fanout_graph(executed, {"bad"})
    _seed_fanout_failure(graph)
    assert executed == ["good", "bad"]

    failed_id = _failed_task_id(graph)
    result = asyncio.run(_recovery().retry_task("t-fan", failed_id, graph))

    assert result["status"] == "retried"
    assert result["task_name"] == "work"
    # 计数桩：失败的 task 重跑一次，已完成的兄弟 task 一次都没有重放
    assert executed == ["good", "bad", "bad"]
    assert executed.count("good") == 1

    snapshot = asyncio.run(graph.aget_state(_cfg("t-fan")))
    assert snapshot.next == ()
    assert sorted(snapshot.values["done"]) == ["bad", "good"]
    assert asyncio.run(_recovery().list_pending_tasks("t-fan", graph)) == []


def test_retry_completed_task_is_refused():
    graph = _build_fanout_graph([], {"bad"})
    _seed_fanout_failure(graph)
    good_id = _completed_task_id(graph)

    with pytest.raises(Exception) as ei:
        asyncio.run(_recovery().retry_task("t-fan", good_id, graph))

    from agentix.application.services.breakpoint_resume import TaskNotRetryableError

    assert isinstance(ei.value, TaskNotRetryableError)
    # 状态未变：失败 task 仍挂着
    snapshot = asyncio.run(graph.aget_state(_cfg("t-fan")))
    assert snapshot.next == ("work",)


def test_retry_unknown_task_and_thread_are_not_found():
    from agentix.application.services.breakpoint_resume import PendingTaskNotFoundError

    graph = _build_fanout_graph([], {"bad"})
    _seed_fanout_failure(graph)

    with pytest.raises(PendingTaskNotFoundError):
        asyncio.run(_recovery().retry_task("t-fan", "nope", graph))
    with pytest.raises(PendingTaskNotFoundError):
        asyncio.run(_recovery().retry_task("nope", "nope", graph))


# ---------------------------------------------------------------------------
# 2. 端点：列表 / 重跑 / 404 / 409
# ---------------------------------------------------------------------------


def test_routes_registered():
    paths = {getattr(r, "path", "") for r in ops_router.router.routes}
    assert "/ops/threads/{thread_id}/tasks" in paths
    assert "/ops/threads/{thread_id}/retry-task" in paths


def test_endpoint_lists_then_retries_failed_task(monkeypatch):
    executed: list = []
    graph = _build_fanout_graph(executed, {"bad"})
    _seed_fanout_failure(graph)
    failed_id = _failed_task_id(graph)

    with _client(monkeypatch, graph) as client:
        listed = client.get("/ops/threads/t-fan/tasks")
        assert listed.status_code == 200
        body = listed.json()
        assert body["thread_id"] == "t-fan"
        assert len(body["tasks"]) == 2
        assert {t["task_id"] for t in body["tasks"]} >= {failed_id}

        # 已完成 task：409 且不重放
        good_id = _completed_task_id(graph)
        conflict = client.post(
            "/ops/threads/t-fan/retry-task", json={"task_id": good_id}
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["error"] == "task_not_retryable"
        assert executed.count("good") == 1

        retried = client.post(
            "/ops/threads/t-fan/retry-task", json={"task_id": failed_id}
        )
        assert retried.status_code == 200
        assert retried.json()["status"] == "retried"
        assert executed == ["good", "bad", "bad"]

        # 重跑完的 task 不再是待执行项
        again = client.post(
            "/ops/threads/t-fan/retry-task", json={"task_id": failed_id}
        )
        assert again.status_code == 404
        assert again.json()["detail"]["error"] == "pending_task_not_found"


def test_endpoint_unknown_thread_and_task_are_404(monkeypatch):
    graph = _build_fanout_graph([], {"bad"})
    _seed_fanout_failure(graph)

    with _client(monkeypatch, graph) as client:
        assert client.get("/ops/threads/nope/tasks").status_code == 404
        missing_task = client.post(
            "/ops/threads/nope/retry-task", json={"task_id": "whatever"}
        )
        assert missing_task.status_code == 404
        assert missing_task.json()["detail"]["error"] == "pending_task_not_found"

        missing = client.post(
            "/ops/threads/t-fan/retry-task", json={"task_id": "whatever"}
        )
        assert missing.status_code == 404
        assert missing.json()["detail"]["thread_id"] == "t-fan"


def test_endpoint_interrupted_thread_points_to_hitl(monkeypatch):
    graph = _build_interrupt_graph()
    asyncio.run(graph.ainvoke({"stage": "start"}, _cfg("t-gate")))

    with _client(monkeypatch, graph) as client:
        listed = client.get("/ops/threads/t-gate/tasks")
        assert listed.status_code == 200
        tasks = listed.json()["tasks"]
        assert len(tasks) == 1 and tasks[0]["interrupts"] == 1

        resp = client.post(
            "/ops/threads/t-gate/retry-task", json={"task_id": tasks[0]["task_id"]}
        )
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["error"] == "thread_interrupted"
        assert detail["resume_endpoint"] == "/ops/hitl/in-loop"
