"""P167（T21）：控制平面运行态落库 + GET 跨重启可查。

对应规划 `docs/design/审批与中断恢复优化规划.md` T21 卡（验收①–④）与项目级口径
`docs/architecture/配置管理.md` §5 的 `RUN_STATE_PERSIST_ENABLED` 行：

① run **创建**与**状态迁移**（pending/running/终态，含取消）落库，终态必须落；
② **重启后** GET 可查历史 run 终态（新进程内存为空 ⇒ 回落库），列表与单查同口径
   （按 run_id 去重合并、内存优先）；
③ 落库 **fail-open**：写库失败时运行照跑、内存态不回滚，失败可观测
   （warning 日志 + 仅 flag-on 存在的 `stats().persist_*` 计数键）；读库失败回落内存面；
④ flag-off **逐位等价**：store 为 None（工厂零调用）、`stats()` 键集合与基线逐字相同、
   响应体形状不变（`meta` 不出现 `thread_id`）。

另钉两条本卡自定的口径：

- `meta.thread_id` 只在 flag-on 且 `options.session_id` 非空时出现（响应体变更登记在
  `docs/项目实现手册.md` Run API 段）；
- 写队列有界，满则**丢最旧并计数**（`persist_dropped`）——观测面不能把进程内存撑穿。

**不依赖真 Postgres**（本机无容器）：sqlite StaticPool 内存 engine + 注入
session_factory，与 T18（`tests/test_p136_hitl_pending_persistence.py`）同风格。
真库上的重启/多副本行为用例属 T28（integration），本文件不冒充。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import agentix.runtime.control_plane as cp_module
from agentix.config import settings
from agentix.infrastructure.persistence import run_state_store as store_module
from agentix.infrastructure.persistence.run_state_store import RunStateStore
from agentix.interfaces.http.v1.runs import require_run_bearer
from agentix.interfaces.http.v1.runs import router as agent_runs_router
from agentix.runtime.control_plane import (
    ControlPlane,
    RunState,
    _RunStateWriter,
    get_control_plane,
    reset_control_plane,
    resolve_run_state_store,
)

#: flag-off 时 `ControlPlane.stats()` 的基线键集合（新增键只在库面装配时出现）
BASELINE_STATS_KEYS = {"total", "active", "by_state", "max_runs"}
#: 库面装配后**多出来**的计数键（无高基数 label，纪律 3）
PERSIST_STATS_KEYS = {
    "persist_queued",
    "persist_written",
    "persist_errors",
    "persist_dropped",
}
#: 单查响应体字段集合（`AgentRun.to_dict()` 的形状；flag-off 必须与基线逐键一致）
RUN_DICT_KEYS = {
    "run_id",
    "agent_name",
    "state",
    "trace_id",
    "created_at",
    "started_at",
    "ended_at",
    "duration_ms",
    "error",
    "cancel_reason",
    "budget",
    "meta",
}


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_plane():
    """每个用例独立的单例平面（防跨用例串扰；写线程随平面一起收口）。"""
    reset_control_plane()
    yield
    reset_control_plane()


@pytest.fixture()
def store() -> RunStateStore:
    """sqlite 内存 store（不触真 Postgres，本机无容器）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    target = RunStateStore(session_factory=sessionmaker(bind=engine, expire_on_commit=False))
    target.ensure_ready()
    return target


@pytest.fixture()
def warning_logs():
    """捕获 loguru WARNING 级消息（fail-open 的可观测面之一）。"""
    records: List[str] = []
    handler_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
    try:
        yield records
    finally:
        logger.remove(handler_id)


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(agent_runs_router)
    # 鉴权由 test_p61 断言，本卡只管运行态落库
    app.dependency_overrides[require_run_bearer] = lambda: None

    @app.middleware("http")
    async def _set_request_id(request, call_next):
        request.state.request_id = "test-rid"
        return await call_next(request)

    return app


@pytest.fixture()
def client():
    with TestClient(_build_app()) as test_client:
        yield test_client


def _simulate_restart(store: RunStateStore) -> ControlPlane:
    """模拟新进程：单例平面重建为**空内存**，库面沿用同一 store。"""
    reset_control_plane()
    cp_module._plane = ControlPlane(run_store=store)
    return cp_module._plane


def _wait_terminal(client: TestClient, run_id: str, *, timeout_s: float = 5.0) -> Dict[str, Any]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        resp = client.get(f"/agents/runs/{run_id}")
        assert resp.status_code == 200
        data = resp.json()["data"]
        if data["state"] in ("succeeded", "failed", "cancelled"):
            return data
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} 未在 {timeout_s}s 内到达终态")


def _wait_result(client: TestClient, run_id: str, *, timeout_s: float = 5.0) -> Dict[str, Any]:
    """等 `_execute_run` 把 result 补写进 meta（终态与 meta 补写是两个时刻）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        data = client.get(f"/agents/runs/{run_id}").json()["data"]
        if "result" in data["meta"]:
            return data
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} 的 meta.result 未在 {timeout_s}s 内落地")


# ---------------------------------------------------------------------------
# 1. 验收①：创建与每次状态迁移落库（含取消终态）
# ---------------------------------------------------------------------------


class TestLifecyclePersistence:
    def test_create_running_and_terminal_are_persisted(self, store):
        plane = ControlPlane(run_store=store)
        run = plane.spawn("probe", trace_id="trace-1", meta={"task_preview": "x"}, depth=2)
        assert plane.flush_persist()

        record = store.load_run(run.run_id)
        assert record is not None
        assert record["state"] == RunState.PENDING.value
        assert record["agent_name"] == "probe"
        assert record["trace_id"] == "trace-1"
        assert record["run_depth"] == 2  # meta.depth 的列化
        assert record["meta"]["task_preview"] == "x"
        assert record["created_at"] == pytest.approx(run.created_at)
        assert record["updated_at"] >= record["created_at"]
        assert record["ended_at"] is None

        assert plane.mark_running(run.run_id) is True
        assert plane.flush_persist()
        record = store.load_run(run.run_id)
        assert record["state"] == RunState.RUNNING.value
        assert record["started_at"] is not None

        assert plane.finish(run.run_id, state=RunState.FAILED, error="boom") is True
        assert plane.flush_persist()
        record = store.load_run(run.run_id)
        assert record["state"] == RunState.FAILED.value
        assert record["error"] == "boom"
        assert record["ended_at"] is not None
        assert record["created_at"] == pytest.approx(run.created_at)  # 起点不被覆盖

    def test_cancel_terminal_is_persisted(self, store):
        plane = ControlPlane(run_store=store)
        run = plane.spawn("probe")
        plane.mark_running(run.run_id)
        assert asyncio.run(plane.cancel(run.run_id, reason="user_cancelled")) is True
        assert plane.flush_persist()
        record = store.load_run(run.run_id)
        assert record["state"] == RunState.CANCELLED.value
        assert record["cancel_reason"] == "user_cancelled"

    def test_meta_rewrite_after_finish_is_persisted(self, store):
        """`finish` 之后补写 meta（result）必须补投：库行与响应面不能各说一套。"""
        plane = ControlPlane(run_store=store)
        run = plane.spawn("probe")
        plane.mark_running(run.run_id)
        plane.finish(run.run_id, state=RunState.SUCCEEDED)
        run.meta["result"] = {"output": "hello"}
        assert plane.persist_run(run.run_id) is True
        assert plane.flush_persist()
        assert store.load_run(run.run_id)["meta"]["result"] == {"output": "hello"}

    def test_upsert_updates_the_same_row(self, store):
        plane = ControlPlane(run_store=store)
        run = plane.spawn("probe")
        plane.mark_running(run.run_id)
        plane.finish(run.run_id, state=RunState.SUCCEEDED)
        assert plane.flush_persist()
        assert [r["run_id"] for r in store.list_runs()] == [run.run_id]  # 一行，不是三行

    def test_store_rejects_state_outside_the_run_state_vocabulary(self, store):
        with pytest.raises(ValueError):
            store.save_run({"run_id": "r-bad", "state": "not-a-run-state"})
        with pytest.raises(ValueError):
            store.save_run({"run_id": "", "state": "pending"})

    def test_list_runs_orders_newest_first_and_filters_by_state(self, store):
        plane = ControlPlane(run_store=store)
        first = plane.spawn("probe")
        plane.finish(first.run_id, state=RunState.SUCCEEDED)
        # Windows 的 time.time() 粒度约 15.6ms：同刻度内两次 spawn 的 created_at 会相等，
        # 排序键分不开。等一下再建第二条，让「倒序」这条断言测的是排序而不是时钟精度。
        time.sleep(0.05)
        second = plane.spawn("probe")
        assert plane.flush_persist()
        assert [r["run_id"] for r in store.list_runs()] == [second.run_id, first.run_id]
        assert [r["run_id"] for r in store.list_runs(state="succeeded")] == [first.run_id]


# ---------------------------------------------------------------------------
# 2. 验收②：重启后 GET 可查历史终态；列表与单查同口径
# ---------------------------------------------------------------------------


class TestRestartFallback:
    def test_get_after_restart_returns_terminal_state(self, store, client):
        cp_module._plane = ControlPlane(run_store=store)
        resp = client.post("/agents/runs", json={"agent": "probe", "task": "hello"})
        assert resp.status_code == 200
        run_id = resp.json()["data"]["run_id"]
        _wait_result(client, run_id)
        assert get_control_plane().flush_persist()

        # 新进程：内存为空（连 run 都没登记过），只能从库回落
        plane = _simulate_restart(store)
        assert plane.status(run_id) is None

        resp = client.get(f"/agents/runs/{run_id}")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert set(data) == RUN_DICT_KEYS  # 回落面与内存面同一响应形状
        assert data["state"] == "succeeded"
        assert data["agent_name"] == "probe"
        assert data["meta"]["result"]["output"] == "hello"
        assert data["budget"] is None  # 预算对象不落库（模型注释口径）

    def test_list_merges_memory_and_db_without_duplicates(self, store, client):
        cp_module._plane = ControlPlane(run_store=store)
        resp = client.post("/agents/runs", json={"agent": "probe", "task": "old"})
        old_id = resp.json()["data"]["run_id"]
        _wait_result(client, old_id)
        assert get_control_plane().flush_persist()

        plane = _simulate_restart(store)
        resp = client.post("/agents/runs", json={"agent": "probe", "task": "new"})
        new_id = resp.json()["data"]["run_id"]
        _wait_terminal(client, new_id)
        assert plane.flush_persist()

        body = client.get("/agents/runs").json()["data"]
        ids = [r["run_id"] for r in body["runs"]]
        assert sorted(ids) == sorted({old_id, new_id})  # 无重复、两边都在
        assert set(body["stats"]) == BASELINE_STATS_KEYS | PERSIST_STATS_KEYS

    def test_memory_entry_wins_over_db_on_merge(self, store, client):
        """同一 run_id 两边都有时以内存为准（库只补历史，不改写现实）。"""
        cp_module._plane = ControlPlane(run_store=store)
        resp = client.post("/agents/runs", json={"agent": "probe", "task": "persisted"})
        run_id = resp.json()["data"]["run_id"]
        _wait_terminal(client, run_id)
        assert get_control_plane().flush_persist()

        plane = _simulate_restart(store)
        # 同名 run_id 重新登记（显式 run_id 的接管场景），内存面与库行刻意不同
        plane.spawn("probe", run_id=run_id, meta={"task_preview": "memory-wins"})
        body = client.get("/agents/runs").json()["data"]
        matched = [r for r in body["runs"] if r["run_id"] == run_id]
        assert len(matched) == 1
        assert matched[0]["meta"]["task_preview"] == "memory-wins"


# ---------------------------------------------------------------------------
# 3. 验收③：fail-open（写失败不阻运行；读失败回内存面；队列有界）
# ---------------------------------------------------------------------------


class _FailingWriteStore:
    """故障注入桩：写必抛，读恒 miss。"""

    def save_run(self, record: Dict[str, Any]) -> None:
        raise RuntimeError("simulated db write failure")

    def load_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        return None

    def list_runs(self, *, state: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        return []


class _FailingReadStore:
    """故障注入桩：读必抛（写正常 no-op）。"""

    def save_run(self, record: Dict[str, Any]) -> None:
        return None

    def load_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        raise RuntimeError("simulated db read failure")

    def list_runs(self, *, state: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        raise RuntimeError("simulated db read failure")


class TestFailOpen:
    def test_write_failure_does_not_block_the_run(self, warning_logs):
        plane = ControlPlane(run_store=_FailingWriteStore())
        run = plane.spawn("probe")
        assert plane.mark_running(run.run_id) is True
        assert plane.finish(run.run_id, state=RunState.SUCCEEDED) is True
        assert plane.flush_persist()

        # 运行照跑、内存态不回滚，失败可观测
        assert plane.status(run.run_id).state is RunState.SUCCEEDED
        stats = plane.stats()
        assert stats["persist_errors"] >= 1
        assert stats["total"] == 1 and stats["active"] == 0
        assert any("persist failed" in line for line in warning_logs)

    def test_read_failure_falls_back_to_memory_face(self, caplog, client):
        cp_module._plane = ControlPlane(run_store=_FailingReadStore())
        with caplog.at_level(logging.WARNING, logger="agentix.interfaces.runs"):
            resp = client.post("/agents/runs", json={"agent": "probe", "task": "mem"})
            run_id = resp.json()["data"]["run_id"]
            _wait_terminal(client, run_id)

            # 单查：内存命中，读库失败不影响
            assert client.get(f"/agents/runs/{run_id}").status_code == 200
            # 单查：内存 miss ⇒ 回落读库抛错 ⇒ 按基线 404（不 500、不造数据）
            assert client.get("/agents/runs/does-not-exist").status_code == 404
            # 列表：库读失败只回内存面
            body = client.get("/agents/runs").json()["data"]
            assert [r["run_id"] for r in body["runs"]] == [run_id]
        assert "store read failed" in caplog.text
        assert "store list failed" in caplog.text

    def test_bounded_queue_drops_oldest_and_counts(self):
        release = threading.Event()
        started = threading.Event()

        class _BlockingStore:
            def save_run(self, record):
                started.set()
                release.wait(5.0)

        writer = _RunStateWriter(_BlockingStore(), max_queue=2)
        try:
            writer.submit({"run_id": "r1"})
            assert started.wait(2.0)
            writer.submit({"run_id": "r2"})
            writer.submit({"run_id": "r3"})
            writer.submit({"run_id": "r4"})  # 队列已满 ⇒ 丢最旧的 r2
            assert writer.snapshot()["dropped"] == 1
        finally:
            release.set()
            writer.close()
        snapshot = writer.snapshot()
        assert snapshot["dropped"] == 1
        assert snapshot["errors"] == 0


# ---------------------------------------------------------------------------
# 4. 验收④：flag-off 逐位等价（纪律 1）
# ---------------------------------------------------------------------------


class TestFlagOffEquivalence:
    def test_resolver_is_the_single_read_point_and_skips_the_builder(self, monkeypatch):
        calls: List[int] = []

        def _forbidden_builder():
            calls.append(1)
            raise AssertionError("flag-off 不得构造 run state store")

        monkeypatch.setattr(store_module, "build_default_run_state_store", _forbidden_builder)
        monkeypatch.setattr(settings, "RUN_STATE_PERSIST_ENABLED", False, raising=False)
        assert resolve_run_state_store() is None
        assert calls == []  # 工厂零调用（装配点本身零新副作用）

    def test_flag_off_plane_has_no_store_and_baseline_stats(self, monkeypatch):
        monkeypatch.setattr(settings, "RUN_STATE_PERSIST_ENABLED", False, raising=False)
        plane = get_control_plane()
        assert plane.run_store is None
        run = plane.spawn("probe")
        plane.mark_running(run.run_id)
        plane.finish(run.run_id, state=RunState.SUCCEEDED)
        assert set(plane.stats()) == BASELINE_STATS_KEYS
        assert plane.persist_run(run.run_id) is False  # 零副作用 no-op
        assert plane.flush_persist() is True

    def test_flag_off_response_shape_is_baseline(self, monkeypatch, client):
        monkeypatch.setattr(settings, "RUN_STATE_PERSIST_ENABLED", False, raising=False)
        resp = client.post(
            "/agents/runs",
            json={"agent": "probe", "task": "s", "options": {"session_id": "s-1"}},
        )
        run_id = resp.json()["data"]["run_id"]
        _wait_terminal(client, run_id)

        data = client.get(f"/agents/runs/{run_id}").json()["data"]
        assert set(data) == RUN_DICT_KEYS
        assert "thread_id" not in data["meta"]  # 响应体变更只在 flag-on 时出现
        body = client.get("/agents/runs").json()["data"]
        assert set(body["stats"]) == BASELINE_STATS_KEYS


# ---------------------------------------------------------------------------
# 5. `meta.thread_id` 的口径：只在 flag-on 且能确定线程时出现
# ---------------------------------------------------------------------------


class TestThreadIdContract:
    def test_thread_id_appears_only_when_session_id_present(self, store, client):
        cp_module._plane = ControlPlane(run_store=store)

        with_session = client.post(
            "/agents/runs",
            json={"agent": "probe", "task": "a", "options": {"session_id": "sess-1"}},
        ).json()["data"]["run_id"]
        without_session = client.post("/agents/runs", json={"agent": "probe", "task": "b"}).json()[
            "data"
        ]["run_id"]
        _wait_terminal(client, with_session)
        _wait_terminal(client, without_session)

        assert (
            client.get(f"/agents/runs/{with_session}").json()["data"]["meta"]["thread_id"]
            == "sess-1"
        )
        assert (
            "thread_id" not in client.get(f"/agents/runs/{without_session}").json()["data"]["meta"]
        )
        # 线程归属也随 run 一起落库（重启后仍能按线程回溯）
        assert get_control_plane().flush_persist()
        assert store.load_run(with_session)["meta"]["thread_id"] == "sess-1"
