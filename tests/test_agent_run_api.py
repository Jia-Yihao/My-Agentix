"""任务级 Agent Run API（Control Plane HTTP 化）测试。

覆盖：
- POST /agents/runs：probe 执行器正常路径 → succeeded + result
- POST 未知执行器 → 400 UNKNOWN_AGENT
- GET /agents/runs/{id}：未知 run → 404；正常 → 状态/预算/结果
- 取消语义：运行中取消 → cancelled 终态；重复取消幂等；终态不被执行器覆盖
- 超时语义：timeout_s 触发 → FAILED（error=timeout after 0.1s）
- GET /agents/runs：列表 + 状态过滤 + stats

零外部依赖：无 DB（runs 路由不触库）、无 LLM（probe 回显执行器）。

A1c：路由已上移至 ``agentix.interfaces.http.v1.runs``（路径
``/api/v1/agents/runs``）；测试内以裸路径挂载（无前缀），鉴权依赖
``require_run_bearer`` 在测试中 override 掉（零 JWT 依赖）。
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentix.runtime.control_plane import reset_control_plane
from agentix.interfaces.http.v1.runs import require_run_bearer
from agentix.interfaces.http.v1.runs import router as agent_runs_router


@pytest.fixture(autouse=True)
def _fresh_plane():
    """每个用例独立的 ControlPlane 单例（防运行记录跨用例串扰）。"""
    reset_control_plane()
    yield
    reset_control_plane()


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(agent_runs_router)

    @app.middleware("http")
    async def _set_request_id(request, call_next):
        request.state.request_id = "test-rid"
        return await call_next(request)

    # 零 JWT 依赖：鉴权本身的行为由 test_p61 断言（无票 ⇒ 401）
    app.dependency_overrides[require_run_bearer] = lambda: None
    with TestClient(app) as test_client:
        yield test_client


def _wait_terminal(client: TestClient, run_id: str, *, timeout_s: float = 5.0) -> dict:
    """轮询到终态（后台任务跑在 portal 循环里，请求间隙持续推进）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        resp = client.get(f"/agents/runs/{run_id}")
        assert resp.status_code == 200
        data = resp.json()["data"]
        if data["state"] in ("succeeded", "failed", "cancelled"):
            return data
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach terminal state within {timeout_s}s")


def _spawn(client: TestClient, **overrides) -> dict:
    body = {"agent": "probe", "task": "示例任务"}
    body.update(overrides)
    resp = client.post("/agents/runs", json=body)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["data"]["status"] == "pending"
    return payload["data"]


def test_probe_run_succeeds_with_result(client):
    run = _spawn(client, options={"delay_s": 0})
    data = _wait_terminal(client, run["run_id"])
    assert data["state"] == "succeeded"
    assert data["meta"]["result"]["output"] == "示例任务"
    assert data["meta"]["result"]["meta"]["executor"] == "probe"
    assert data["duration_ms"] >= 0


def test_unknown_agent_returns_400(client):
    resp = client.post("/agents/runs", json={"agent": "nope", "task": "x"})
    assert resp.status_code == 400
    assert resp.json()["error_code"] == "UNKNOWN_AGENT"


def test_get_unknown_run_returns_404(client):
    resp = client.get("/agents/runs/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "AGENT_RUN_NOT_FOUND"


def test_run_with_step_budget_records_budget(client):
    run = _spawn(client, max_steps=10)
    data = _wait_terminal(client, run["run_id"])
    assert data["budget"] is not None
    assert data["budget"]["limit"] == 10
    assert data["budget"]["name"] == "agent_run_steps"


def test_cancel_running_run(client):
    run = _spawn(client, options={"delay_s": 5})
    time.sleep(0.2)  # 让后台任务进入 running（delay 睡眠中）
    resp = client.post(f"/agents/runs/{run['run_id']}/cancel")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["state"] == "cancelled"
    assert data["cancel_reason"] == "user_cancelled"

    # 终态不被覆盖：等待原超时窗口过后状态仍是 cancelled
    time.sleep(0.3)
    again = client.get(f"/agents/runs/{run['run_id']}").json()["data"]
    assert again["state"] == "cancelled"

    # 重复取消幂等：终态返回 False，但 API 仍 200 且状态不变
    resp2 = client.post(f"/agents/runs/{run['run_id']}/cancel")
    assert resp2.status_code == 200
    assert resp2.json()["data"]["state"] == "cancelled"


def test_cancel_unknown_run_returns_404(client):
    resp = client.post("/agents/runs/does-not-exist/cancel")
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "AGENT_RUN_NOT_FOUND"


def test_timeout_fails_run(client):
    run = _spawn(client, options={"delay_s": 3}, timeout_s=0.1)
    data = _wait_terminal(client, run["run_id"])
    assert data["state"] == "failed"
    assert "timeout after 0.1s" in (data["error"] or "")


def test_executor_exception_fails_run(client):
    from agentix.application.services.agent_executors import register_executor

    async def boom(task, options):
        raise RuntimeError("exploded")

    register_executor("boom", boom, overwrite=True)
    resp = client.post("/agents/runs", json={"agent": "boom", "task": "x"})
    assert resp.status_code == 200
    data = _wait_terminal(client, resp.json()["data"]["run_id"])
    assert data["state"] == "failed"
    assert data["error"] == "exploded"


def test_list_runs_with_state_filter_and_stats(client):
    r1 = _spawn(client, task="任务一")["run_id"]
    r2 = _spawn(client, task="任务二", options={"delay_s": 5})["run_id"]
    _wait_terminal(client, r1)

    resp = client.get("/agents/runs")
    assert resp.status_code == 200
    payload = resp.json()["data"]
    ids = {r["run_id"] for r in payload["runs"]}
    assert {r1, r2} <= ids
    assert payload["stats"]["total"] >= 2
    assert payload["stats"]["by_state"].get("succeeded", 0) >= 1

    resp = client.get("/agents/runs", params={"state": "running"})
    assert resp.status_code == 200
    running = resp.json()["data"]["runs"]
    assert all(r["state"] == "running" for r in running)
    assert any(r["run_id"] == r2 for r in running)

    # 非法过滤 → 400
    resp = client.get("/agents/runs", params={"state": "bogus"})
    assert resp.status_code == 400
    assert resp.json()["error_code"] == "VALIDATION_ERROR"
