"""T20：Run API `Idempotency-Key` header + 200 复用——契约的可执行判据。

契约真源：`docs/architecture/接口幂等契约.md`（Q8 裁决：同键重放返
200 复用原 run）。覆盖：

- 无 header ⇒ 与现状逐位等价（响应形状/状态码/创建行为对齐基线，不加隐式键）；
- 同键**先后**重放 ⇒ 200 + 同一 run_id，副作用只执行一次（桩计数证明）；
- 同键**并发**重放 ⇒ 只创建一个运行（asyncio.gather 打竞态；另有存储层
  多线程 claim 直测，两层都钉住"检查+登记"原子性）；
- TTL / 上限清理口径可判定（注入假时钟，不让测试真等）；
- 键合法性错误不回放键内容；失败请求（UNKNOWN_AGENT）不占用键；
- 幂等命中不静默（日志留痕）且键只以指纹形式出现；
- 键内容不进指标 label（纪律 3 高基数禁令）。

零外部依赖：无 DB、无 LLM（计数桩执行器）；不依赖真实时间流逝。
分层提醒：本文件验证的是**客户端重试幂等**，与审批侧"一次批准一次执行"
（human_review）是两层，互不援引。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time as _time
from typing import Any, Dict, List

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agentix.interfaces.http.v1.runs as runs_mod
from agentix.application.services.agent_executors import register_executor
from agentix.interfaces.http.v1.runs import require_run_bearer
from agentix.interfaces.http.v1.runs import router as agent_runs_router
from agentix.observability.metrics import get_metrics
from agentix.runtime.control_plane import get_control_plane, reset_control_plane


@pytest.fixture(autouse=True)
def _fresh_state():
    """每个用例独立的 ControlPlane + 幂等键存储（防跨用例串扰）。"""
    reset_control_plane()
    runs_mod.reset_run_idempotency_store()
    yield
    reset_control_plane()
    runs_mod.reset_run_idempotency_store()


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(agent_runs_router)

    @app.middleware("http")
    async def _set_request_id(request, call_next):
        request.state.request_id = "test-rid"
        return await call_next(request)

    # 零 JWT 依赖：鉴权行为由 test_p61 断言，本卡只管幂等
    app.dependency_overrides[require_run_bearer] = lambda: None
    return app


@pytest.fixture()
def client():
    with TestClient(_build_app()) as test_client:
        yield test_client


@pytest.fixture()
def counter_executor() -> List[str]:
    """计数桩执行器：每次调用登记一条，证明副作用没有被二次执行。"""
    calls: List[str] = []

    async def _counter(task: str, options: Dict[str, Any]) -> Dict[str, Any]:
        calls.append(task)
        await asyncio.sleep(0.01)
        return {"output": task}

    register_executor("counter", _counter, overwrite=True)
    return calls


def _wait_terminal(client: TestClient, run_id: str, *, timeout_s: float = 5.0) -> dict:
    deadline = _time.time() + timeout_s
    while _time.time() < deadline:
        resp = client.get(f"/agents/runs/{run_id}")
        assert resp.status_code == 200
        data = resp.json()["data"]
        if data["state"] in ("succeeded", "failed", "cancelled"):
            return data
        _time.sleep(0.02)
    raise AssertionError(f"run {run_id} 未在 {timeout_s}s 内到达终态")


# ---------------------------------------------------------------------------
# ③ 无 header ⇒ 与现状逐位等价（纪律 1）
# ---------------------------------------------------------------------------


def test_no_header_matches_baseline(client, counter_executor):
    """无 header：响应形状、状态码、创建行为与基线一致，且不落任何隐式键。"""
    resp1 = client.post("/agents/runs", json={"agent": "counter", "task": "t1"})
    assert resp1.status_code == 200
    payload1 = resp1.json()
    # 信封形状逐字段对齐基线（既有 test_agent_run_api 的判据）
    assert set(payload1.keys()) == {"request_id", "data"}
    assert set(payload1["data"].keys()) == {"run_id", "agent", "status"}
    assert payload1["data"]["status"] == "pending"
    assert payload1["data"]["agent"] == "counter"
    rid1 = payload1["data"]["run_id"]
    _wait_terminal(client, rid1)

    # 连续两个无 header 请求创建两个不同运行（= 基线"重试必重复创建"行为，
    # 幂等键缺省时不得出现任何去重）
    resp2 = client.post("/agents/runs", json={"agent": "counter", "task": "t1"})
    assert resp2.status_code == 200
    rid2 = resp2.json()["data"]["run_id"]
    assert rid2 != rid1
    _wait_terminal(client, rid2)

    assert counter_executor == ["t1", "t1"]  # 两次执行，基线语义
    # 无 header 路径不向幂等存储写入任何东西
    assert runs_mod.get_run_idempotency_store().stats()["entries"] == 0


# ---------------------------------------------------------------------------
# ② 同键先后重放 ⇒ 200 + 复用原 run_id，副作用只执行一次
# ---------------------------------------------------------------------------


def test_same_key_sequential_replay_reuses_run(client, counter_executor):
    headers = {"Idempotency-Key": "key-A"}
    resp1 = client.post(
        "/agents/runs", json={"agent": "counter", "task": "solo"}, headers=headers
    )
    assert resp1.status_code == 200
    rid = resp1.json()["data"]["run_id"]

    # 终态前后各重放一次：都返 200、同一 run_id、响应形状不变
    for _ in range(2):
        replay = client.post(
            "/agents/runs", json={"agent": "counter", "task": "solo"}, headers=headers
        )
        assert replay.status_code == 200
        body = replay.json()
        assert set(body["data"].keys()) == {"run_id", "agent", "status"}
        assert body["data"]["run_id"] == rid
        _time.sleep(0.02)

    _wait_terminal(client, rid)
    assert counter_executor == ["solo"]  # 副作用只执行一次
    # 不同键 ⇒ 独立运行（键之间不去重）
    resp_b = client.post(
        "/agents/runs", json={"agent": "counter", "task": "solo"},
        headers={"Idempotency-Key": "key-B"},
    )
    assert resp_b.json()["data"]["run_id"] != rid


# ---------------------------------------------------------------------------
# ② 同键并发 ⇒ 只创建一个运行（检查+登记原子性）
# ---------------------------------------------------------------------------


def test_concurrent_same_key_creates_only_one_run(counter_executor):
    """asyncio.gather 打竞态：5 个同键请求并发 ⇒ 1 个运行、1 次执行。"""

    async def _scenario() -> List[httpx.Response]:
        transport = httpx.ASGITransport(app=_build_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            responses = await asyncio.gather(
                *[
                    ac.post(
                        "/agents/runs",
                        json={"agent": "counter", "task": "race"},
                        headers={"Idempotency-Key": "race-key"},
                    )
                    for _ in range(5)
                ]
            )
            # 等后台 worker 跑完，计数才可判定
            deadline = _time.monotonic() + 5.0
            while len(counter_executor) < 1 and _time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.05)
            return responses

    responses = asyncio.run(_scenario())
    assert all(r.status_code == 200 for r in responses)
    run_ids = {r.json()["data"]["run_id"] for r in responses}
    assert len(run_ids) == 1, f"并发同键创建了多个运行：{run_ids}"
    assert len(counter_executor) == 1, "同键并发导致副作用二次执行"
    assert len(get_control_plane().list_runs()) == 1


def test_store_claim_is_atomic_under_threads():
    """存储层直测：8 线程同时 claim 同一键 ⇒ 工厂只被调用一次（锁守卫）。"""
    store = runs_mod.RunIdempotencyStore()
    barrier = threading.Barrier(8)
    made: List[str] = []
    results: List[Any] = []
    results_lock = threading.Lock()

    def _factory():
        made.append("x")
        _time.sleep(0.01)  # 放大竞态窗口：无锁时多线程会各自进工厂
        return ("run-from-factory", "counter")

    def _worker():
        barrier.wait()
        outcome = store.claim("hot-key", _factory)
        with results_lock:
            results.append(outcome)

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(made) == 1
    assert all(r.run_id == "run-from-factory" for r in results)
    assert sum(1 for r in results if r.replayed) == 7


# ---------------------------------------------------------------------------
# ④ TTL / 上限清理口径（假时钟注入，不真等）
# ---------------------------------------------------------------------------


def test_ttl_expiry_reopens_key(client, counter_executor):
    clock = {"t": 1_000.0}
    runs_mod.reset_run_idempotency_store(time_fn=lambda: clock["t"])

    headers = {"Idempotency-Key": "ttl-key"}
    rid1 = client.post(
        "/agents/runs", json={"agent": "counter", "task": "a"}, headers=headers
    ).json()["data"]["run_id"]
    clock["t"] += runs_mod.IDEMPOTENCY_TTL_S + 1.0
    rid2 = client.post(
        "/agents/runs", json={"agent": "counter", "task": "a"}, headers=headers
    ).json()["data"]["run_id"]

    assert rid2 != rid1  # 契约：过期键视同不存在 ⇒ 新建
    assert runs_mod.get_run_idempotency_store().stats()["expired_swept"] >= 1


def test_max_entries_evicts_oldest_fifo(client, counter_executor):
    runs_mod.reset_run_idempotency_store(max_entries=2)
    rids = {}
    for key in ("k1", "k2", "k3"):
        rids[key] = client.post(
            "/agents/runs", json={"agent": "counter", "task": key},
            headers={"Idempotency-Key": key},
        ).json()["data"]["run_id"]

    # k1 已被 FIFO 逐出 ⇒ 同键重试按契约新建（不谎称仍在窗内）
    again_k1 = client.post(
        "/agents/runs", json={"agent": "counter", "task": "k1"},
        headers={"Idempotency-Key": "k1"},
    ).json()["data"]["run_id"]
    assert again_k1 != rids["k1"]

    # k3 仍在窗内 ⇒ 复用
    hit_k3 = client.post(
        "/agents/runs", json={"agent": "counter", "task": "k3"},
        headers={"Idempotency-Key": "k3"},
    ).json()["data"]["run_id"]
    assert hit_k3 == rids["k3"]
    assert runs_mod.get_run_idempotency_store().stats()["evicted"] >= 1


# ---------------------------------------------------------------------------
# 键合法性：拒绝但不回放；失败请求不占用键
# ---------------------------------------------------------------------------


def test_invalid_key_rejected_without_echoing(client, counter_executor):
    sentinel = "SECRET-SENTINEL-VALUE-DO-NOT-ECHO"
    too_long = sentinel + "x" * (runs_mod.IDEMPOTENCY_MAX_KEY_CHARS + 10)

    empty = client.post(
        "/agents/runs", json={"agent": "counter", "task": "a"},
        headers={"Idempotency-Key": "   "},
    )
    assert empty.status_code == 400
    assert empty.json()["error_code"] == "INVALID_IDEMPOTENCY_KEY"

    over = client.post(
        "/agents/runs", json={"agent": "counter", "task": "a"},
        headers={"Idempotency-Key": too_long},
    )
    assert over.status_code == 400
    assert over.json()["error_code"] == "INVALID_IDEMPOTENCY_KEY"
    # 纪律 6 ③ 精神：错误响应不回放输入
    assert sentinel not in over.text

    # 被拒请求不占用键、不创建运行
    stats = runs_mod.get_run_idempotency_store().stats()
    assert stats["entries"] == 0
    assert stats["runs_created"] == 0


def test_failed_create_does_not_burn_key(client, counter_executor):
    """UNKNOWN_AGENT（登记前失败）不占键：同键随后带合法 agent 重试应成功创建。"""
    headers = {"Idempotency-Key": "reusable-key"}
    bad = client.post("/agents/runs", json={"agent": "nope", "task": "a"}, headers=headers)
    assert bad.status_code == 400
    assert bad.json()["error_code"] == "UNKNOWN_AGENT"

    good = client.post(
        "/agents/runs", json={"agent": "counter", "task": "a"}, headers=headers
    )
    assert good.status_code == 200
    _wait_terminal(client, good.json()["data"]["run_id"])
    assert len(counter_executor) == 1


# ---------------------------------------------------------------------------
# 可观测性：命中不静默、键只以指纹出现、不进指标 label
# ---------------------------------------------------------------------------


def test_replay_hit_is_logged_with_fingerprint_not_raw_key(client, caplog):
    key = "user-secret-key-abc"
    headers = {"Idempotency-Key": key}
    client.post("/agents/runs", json={"agent": "counter", "task": "a"}, headers=headers)
    fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]

    with caplog.at_level(logging.INFO, logger="agentix.interfaces.runs"):
        client.post("/agents/runs", json={"agent": "counter", "task": "a"}, headers=headers)

    hit_lines = [r.getMessage() for r in caplog.records if "idempotency" in r.getMessage().lower()]
    assert hit_lines, "幂等命中必须可观测（不得静默）"
    assert any(fingerprint in line for line in hit_lines)
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert key not in joined  # 键原文不进日志


def test_key_never_appears_in_metric_labels(client):
    sentinel = "metric-poison-sentinel-9f3c"
    headers = {"Idempotency-Key": sentinel}
    client.post("/agents/runs", json={"agent": "counter", "task": "a"}, headers=headers)
    client.post("/agents/runs", json={"agent": "counter", "task": "a"}, headers=headers)
    assert sentinel.encode("utf-8") not in get_metrics()
