"""A1 统一执行链批次（p61+）——Run source 统一 / 单 owner / 退出契约 / Run API 上移 / metadata 死重过滤。

覆盖（对应 docs/migration/架构重构_统一执行链方案_v2.md Stage A · A1）：

1. **A1a** ControlPlane：``spawn(run_id=...)`` 外部指定 ID + 重复拒绝；
   ``bind`` 单 owner（活任务不改绑、终了可改绑）；流式路径
   （``_stream_events_impl``）入控制面——正常收口 SUCCEEDED、断连收口
   CANCELLED(client_disconnected)、异常收口 FAILED、idle 超时收口
   FAILED(stream_failure)。
2. **A1b** 退出契约：``LoopResult.exit_status/exit_reason/truncated`` 与
   ``TaskOutcome`` 字段派生（domain.status.ExitStatus/ExitReason）。
3. **A1c** Run API 上移：``/api/v1/agents/runs`` 挂载于 v1、retrieval 聚合
   不再含 runs、无票 401（``require_run_bearer``）、执行器注册表含
   ``probe`` + ``chat``（真实图执行器映射契约）。
4. **A1d** metadata 死重过滤：``METADATA_EXCLUDE_KEYS`` 在落库
   （``save_message``）与响应（``_public_metadata``）两端同口径生效。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentix.application.services import chat_service as cs_mod
from agentix.application.services.agent_executors import (
    chat_executor,
    executor_names,
)
from agentix.runtime.control_plane import (
    get_control_plane,
    reset_control_plane,
)
from agentix.orchestration.orchestrator import LoopResult
from agentix.application.services.chat_service import (
    METADATA_EXCLUDE_KEYS,
    ChatService,
)
from agentix.application.services.task_retry import TaskOutcome
from agentix.domain.agent.types import ContextSnapshot, Plan
from agentix.domain.runtime.lifecycle import ExitReason, ExitStatus


@pytest.fixture(autouse=True)
def _fresh_plane():
    """每个用例独立的 ControlPlane 单例（防运行记录跨用例串扰）。"""
    reset_control_plane()
    yield
    reset_control_plane()


# ---------------------------------------------------------------------------
# A1a · ControlPlane spawn(run_id) / bind 单 owner
# ---------------------------------------------------------------------------


class TestSpawnExplicitRunId:
    def test_spawn_with_explicit_run_id(self):
        plane = get_control_plane()
        run = plane.spawn("kg_graph", run_id="fixed-run-1")
        assert run.run_id == "fixed-run-1"
        assert plane.status("fixed-run-1") is run

    def test_spawn_duplicate_run_id_rejected(self):
        plane = get_control_plane()
        plane.spawn("kg_graph", run_id="dup-1")
        with pytest.raises(ValueError, match="already registered"):
            plane.spawn("kg_graph", run_id="dup-1")

    def test_spawn_default_run_id_is_generated(self):
        plane = get_control_plane()
        a = plane.spawn("kg_graph")
        b = plane.spawn("kg_graph")
        assert a.run_id and b.run_id and a.run_id != b.run_id


class TestBindSingleOwner:
    @pytest.mark.asyncio
    async def test_live_task_not_rebindable(self):
        plane = get_control_plane()
        run = plane.spawn("x")
        t1 = asyncio.ensure_future(asyncio.sleep(0.05))
        assert plane.bind(run.run_id, t1) is True

        t2 = asyncio.ensure_future(asyncio.sleep(0.05))
        assert plane.bind(run.run_id, t2) is False, "活任务不得被改绑"

        await t1
        assert plane.bind(run.run_id, t2) is True, "既有任务终了后可改绑（接管）"
        await t2

    @pytest.mark.asyncio
    async def test_same_task_rebind_is_idempotent(self):
        plane = get_control_plane()
        run = plane.spawn("x")
        t1 = asyncio.ensure_future(asyncio.sleep(0.01))
        assert plane.bind(run.run_id, t1) is True
        assert plane.bind(run.run_id, t1) is True, "同任务重复绑定是幂等的"
        await t1


# ---------------------------------------------------------------------------
# A1a · 流式路径入控制面
# ---------------------------------------------------------------------------


def _done_event() -> Dict[str, Any]:
    """最外层图结束事件（node_name 缺省 ⇒ 触发 done 帧）。"""
    return {
        "event": "on_chain_end",
        "name": "LangGraph",
        "data": {"output": {"messages": []}},
        "metadata": {},
    }


class _StubStreamGraph:
    def __init__(self, events: Optional[List[Dict[str, Any]]] = None, *, hang: bool = False):
        self._events = events or []
        self._hang = hang

    def astream_events(self, input_state, *, config=None, version=None):
        events = self._events
        hang = self._hang

        async def _gen():
            if hang:
                await asyncio.Event().wait()  # 永不产出（触发 idle 超时）
            for ev in events:
                yield ev

        return _gen()


def _stream_service(events=None, *, hang=False) -> ChatService:
    stub = _StubStreamGraph(events, hang=hang)
    return ChatService(graph_getter=lambda: stub)


def _stream_runs():
    return [
        r
        for r in get_control_plane().list_runs()
        if r.meta.get("channel") == "stream"
    ]


class TestStreamRunRegistration:
    @pytest.mark.asyncio
    async def test_stream_normal_end_records_succeeded(self):
        svc = _stream_service([_done_event()])
        events = [ev async for ev in svc._stream_events_impl("问题", "sess-p61-ok")]
        assert any(ev["type"] == "done" for ev in events)

        runs = _stream_runs()
        assert len(runs) == 1
        run = runs[0]
        assert run.agent_name == "kg_graph"
        assert run.meta.get("session_id") == "sess-p61-ok"
        assert run.state.value == "succeeded"
        assert run.error is None
        assert run.started_at is not None and run.ended_at is not None

    @pytest.mark.asyncio
    async def test_client_disconnect_records_cancelled(self):
        svc = _stream_service([_done_event()])
        gen = svc._stream_events_impl("问题", "sess-p61-disc")
        first = await gen.__anext__()
        assert first["type"] == "metadata"
        await gen.aclose()  # 客户端断连语义

        runs = _stream_runs()
        assert len(runs) == 1
        run = runs[0]
        assert run.state.value == "cancelled"
        assert run.error == "client_disconnected"

    @pytest.mark.asyncio
    async def test_graph_exception_records_failed(self):
        class _BoomGraph:
            def astream_events(self, input_state, *, config=None, version=None):
                raise RuntimeError("boom-graph")  # 调用点同步抛出（非 async gen）

        svc = ChatService(graph_getter=lambda: _BoomGraph())
        events = [ev async for ev in svc._stream_events_impl("问题", "sess-p61-err")]
        assert any(ev["type"] == "error" for ev in events)
        assert any(ev["type"] == "done" for ev in events)

        runs = _stream_runs()
        assert len(runs) == 1
        run = runs[0]
        assert run.state.value == "failed"
        assert "boom-graph" in (run.error or "")

    @pytest.mark.asyncio
    async def test_idle_timeout_records_failed(self, monkeypatch):
        from agentix.config import settings

        monkeypatch.setattr(settings, "STREAM_IDLE_TIMEOUT", 0.05)
        svc = _stream_service(hang=True)
        events = [ev async for ev in svc._stream_events_impl("问题", "sess-p61-idle")]
        assert any(ev["type"] == "error" for ev in events)

        runs = _stream_runs()
        assert len(runs) == 1
        run = runs[0]
        assert run.state.value == "failed"
        assert "idle timeout" in (run.error or "")

    @pytest.mark.asyncio
    async def test_run_recorded_exactly_once_per_stream(self):
        svc = _stream_service([_done_event()])
        _ = [ev async for ev in svc._stream_events_impl("问题", "sess-a")]
        _ = [ev async for ev in svc._stream_events_impl("问题", "sess-b")]
        runs = _stream_runs()
        assert len(runs) == 2
        assert {r.meta.get("session_id") for r in runs} == {"sess-a", "sess-b"}


# ---------------------------------------------------------------------------
# A1b · 退出契约
# ---------------------------------------------------------------------------


def _loop_result(terminal: str) -> LoopResult:
    return LoopResult(
        task_id="p61",
        final="",
        passed=False,
        plan=Plan(),
        context=ContextSnapshot(prompt=""),
        terminal=terminal,
    )


class TestLoopResultExitContract:
    def test_completed(self):
        r = _loop_result("completed")
        assert r.exit_status == ExitStatus.COMPLETED
        assert r.exit_status == "completed"  # str 枚举可与裸串比较
        assert r.exit_reason == ExitReason.COMPLETED
        assert r.truncated is False

    def test_budget_exceeded_maps_to_max_iterations(self):
        r = _loop_result("budget_exceeded")
        assert r.exit_status == ExitStatus.FAILED
        assert r.exit_reason == ExitReason.MAX_ITERATIONS
        assert r.truncated is True

    @pytest.mark.parametrize("terminal", ["verify_failed", "failed", "context_build_failed"])
    def test_other_failures_map_to_error(self, terminal):
        r = _loop_result(terminal)
        assert r.exit_status == ExitStatus.FAILED
        assert r.exit_reason == ExitReason.ERROR
        assert r.truncated is False


class TestTaskOutcomeExitContract:
    def test_ok_derives_completed(self):
        o = TaskOutcome(name="w", ok=True, attempts=1)
        assert o.exit_status == ExitStatus.COMPLETED
        assert o.exit_reason == ExitReason.COMPLETED
        assert o.truncated is False

    def test_non_retryable_failure_derives_error(self):
        o = TaskOutcome(name="w", ok=False, error="ValueError: x", retryable=False)
        assert o.exit_status == ExitStatus.FAILED
        assert o.exit_reason == ExitReason.ERROR
        assert o.truncated is False

    def test_retryable_exhausted_derives_max_iterations(self):
        o = TaskOutcome(name="w", ok=False, error="TimeoutError: x", retryable=True, attempts=3)
        assert o.exit_status == ExitStatus.FAILED
        assert o.exit_reason == ExitReason.MAX_ITERATIONS
        assert o.truncated is True

    def test_explicit_exit_fields_not_overridden(self):
        o = TaskOutcome(
            name="w",
            ok=False,
            retryable=False,
            exit_status=ExitStatus.INTERRUPTED,
            exit_reason=ExitReason.INTERRUPTED,
        )
        assert o.exit_status == ExitStatus.INTERRUPTED
        assert o.exit_reason == ExitReason.INTERRUPTED

    def test_to_dict_exposes_exit_contract(self):
        d = TaskOutcome(name="w", ok=True, attempts=1).to_dict()
        assert d["exit_status"] == "completed"
        assert d["exit_reason"] == "completed"
        assert d["truncated"] is False

    def test_exclude_keys_constant_contents(self):
        assert METADATA_EXCLUDE_KEYS == frozenset(
            {"agent_state", "runtime", "budget", "completion"}
        )


# ---------------------------------------------------------------------------
# A1c · Run API 上移 + 执行器注册表
# ---------------------------------------------------------------------------


class TestRunApiMovedToV1:
    def test_v1_router_exposes_runs_routes(self):
        from agentix.interfaces.http.v1 import api_router
        from route_introspect import iter_effective_routes

        paths = {getattr(r, "path", "") for r in iter_effective_routes(api_router.routes)}
        assert "/agents/runs" in paths
        assert "/agents/runs/{run_id}" in paths

    def test_retrieval_aggregate_no_longer_mounts_runs(self):
        from agentix.retrieval.api.routers import router as retrieval_router
        from route_introspect import iter_effective_routes

        paths = [
            getattr(r, "path", "") for r in iter_effective_routes(retrieval_router.routes)
        ]
        assert not any("runs" in p for p in paths), "Run API 已上移，retrieval 不得再挂载"

    def test_run_api_rejects_missing_bearer(self):
        from agentix.interfaces.http.v1.runs import router as runs_router

        app = FastAPI()
        app.include_router(runs_router)
        with TestClient(app) as client:
            resp = client.get("/agents/runs")
            assert resp.status_code == 401

    def test_run_api_rejects_invalid_bearer(self):
        from agentix.interfaces.http.v1.runs import router as runs_router

        app = FastAPI()
        app.include_router(runs_router)
        with TestClient(app) as client:
            resp = client.get(
                "/agents/runs", headers={"Authorization": "Bearer not-a-jwt"}
            )
            assert resp.status_code == 401


class TestExecutorRegistry:
    def test_registry_has_probe_and_chat(self):
        names = executor_names()
        assert "probe" in names
        assert "chat" in names

    @pytest.mark.asyncio
    async def test_chat_executor_maps_ask_result(self, monkeypatch):
        class _StubService:
            def __init__(self):
                self.calls: List[Any] = []

            async def ask(self, message, session_id, image_path=None,
                          file_path=None, ingest_incremental=None):
                self.calls.append((message, session_id))
                return {
                    "message": "回答正文",
                    "route": "general",
                    "route_logic": "x",
                    "sources": [{"document_id": "d1"}],
                    "status": "ok",
                    "error_code": None,
                    "metadata": {},
                }

        stub = _StubService()
        monkeypatch.setattr(cs_mod, "get_chat_service", lambda: stub)
        out = await chat_executor("问题", {"session_id": "s-p61"})

        assert stub.calls == [("问题", "s-p61")]
        assert out["output"] == "回答正文"
        assert out["meta"]["executor"] == "chat"
        assert out["meta"]["session_id"] == "s-p61"
        assert out["meta"]["route"] == "general"
        assert out["meta"]["sources"] == [{"document_id": "d1"}]

    @pytest.mark.asyncio
    async def test_chat_executor_generates_session_when_missing(self, monkeypatch):
        seen: Dict[str, Any] = {}

        class _StubService:
            async def ask(self, message, session_id, **kwargs):
                seen["session_id"] = session_id
                return {"message": "ok", "status": "ok"}

        monkeypatch.setattr(cs_mod, "get_chat_service", lambda: _StubService())
        out = await chat_executor("问题", {})
        assert seen["session_id"], "缺省 session_id 应自动生成"
        assert out["meta"]["session_id"] == seen["session_id"]


# ---------------------------------------------------------------------------
# A1d · metadata 死重过滤（落库 + 响应同口径）
# ---------------------------------------------------------------------------

_DEAD_AND_ALIVE = {
    "agent_state": {"huge": "graph-state"},
    "runtime": {"steps": []},
    "budget": {"limit": 0},
    "completion": {"signal": "complete"},
    "foo": 1,
    "sources": [{"document_id": "d1"}],
}


class TestMetadataDeadWeightFilter:
    @pytest.mark.asyncio
    async def test_save_message_filters_dead_keeps_alive(self):
        svc = ChatService(session_repo=MagicMock(), message_repo=MagicMock())
        svc._message_repo.get_latest_by_session.return_value = None
        created = MagicMock()
        created.id = "m-p61"
        svc._message_repo.create.return_value = created

        await svc.save_message(
            MagicMock(), "s-p61", "内容", is_user=False, route="general",
            metadata=dict(_DEAD_AND_ALIVE),
        )

        obj_in = svc._message_repo.create.call_args.kwargs["obj_in"]
        md = obj_in["message_metadata"]
        for key in METADATA_EXCLUDE_KEYS:
            assert key not in md, f"死重键 {key} 不得落库"
        assert md["foo"] == 1
        assert md["sources"] == [{"document_id": "d1"}]
        assert md["route"] == "general"

    def test_public_metadata_filters_dead_keeps_alive(self):
        from agentix.interfaces.http.v1.chat import _public_metadata

        out = _public_metadata(dict(_DEAD_AND_ALIVE))
        assert out == {"foo": 1, "sources": [{"document_id": "d1"}]}

    def test_public_metadata_passthrough_non_dict(self):
        from agentix.interfaces.http.v1.chat import _public_metadata

        assert _public_metadata(None) is None
        assert _public_metadata("raw") == "raw"
