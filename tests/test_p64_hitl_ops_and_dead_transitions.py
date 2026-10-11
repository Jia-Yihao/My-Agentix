"""A4：HITL 生产审批决策面 + application/runtime 直删 + 死转移处置。

三块对应 v2 §4 的 A4 行：

1. **HITL WAITING_APPROVAL 生产空转修复**：``bridge.decide`` 此前零生产
   调用方（挂起即不可解）。ops 面补上决策通道::

       GET  /ops/hitl/approvals                     列待审 + 观测 stats
       POST /ops/hitl/approvals/{sidecar_request_id} 四裁决回写闸门

2. **application/runtime 直删**：6 文件全仓 0 importer（v2 §1 已核实），
   删除后不存在回潮（路径断言）。

3. **死转移处置**：``PAUSED_FOR_APPROVAL`` 自 Phase 8 起语义由
   ``WAITING_APPROVAL`` 承担，生产/测试零进入 = 死转移——状态成员与
   ``ALLOWED_TRANSITIONS`` 连边一并移除，审批流收敛到单一状态。

零外部依赖：不触库、不触 LLM；全局闸门/桥单例逐用例重建。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentix.policies.hitl.hitl_bridge import (
    get_hitl_bridge,
    reset_hitl_bridge,
)
from agentix.tools.gateway.human_review import (
    ReviewDecision,
    ReviewRequired,
    declared_level_for,
    mark_decision_surface,
    reset_review_gate,
)
from agentix.config import settings
from agentix.interfaces.http import ops_router
from agentix.infrastructure.core.security import verify_api_key
from agentix.runtime.lifecycle import ALLOWED_TRANSITIONS, LifecycleManager
from agentix.runtime.state import AgentState, AgentStatus

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fresh_hitl_stack(monkeypatch):
    """全局桥 + 全局闸门逐用例重建（flag 开 = 闸门启用 = 生产等价接线）。"""
    reset_hitl_bridge()
    reset_review_gate()
    monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", True)
    mark_decision_surface()  # ops 决策端点即决策面（避免「开了却无人可批」告警）
    yield
    reset_hitl_bridge()
    reset_review_gate()


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(ops_router.router)
    app.dependency_overrides[verify_api_key] = lambda: None
    with TestClient(app) as test_client:
        yield test_client


def _suspend(tool_name: str = "delete_records", args: dict | None = None):
    """不经 executor 直接触发闸门拦截并挂 Sidecar（返回 sidecar_request_id）。"""
    args = args if args is not None else {"table": "orders"}
    gate = get_hitl_bridge().gate
    with pytest.raises(ReviewRequired) as ei:
        gate.preflight(tool_name, args, declared_level=declared_level_for("L2"))
    suspend = asyncio.run(get_hitl_bridge().open_from_gate(ei.value.request, dict(args)))
    return suspend, ei.value.request


# ---------------------------------------------------------------------------
# 1. 路由注册
# ---------------------------------------------------------------------------


def test_hitl_approval_routes_registered():
    paths = {getattr(r, "path", "") for r in ops_router.router.routes}
    assert "/ops/hitl/approvals" in paths
    assert "/ops/hitl/approvals/{sidecar_request_id}" in paths


# ---------------------------------------------------------------------------
# 2. 待审面 / 四裁决端点
# ---------------------------------------------------------------------------


def test_ops_pending_empty_by_default(client):
    resp = client.get("/ops/hitl/approvals")
    assert resp.status_code == 200
    body = resp.json()
    assert body["pending"] == []
    assert body["stats"]["enabled"] is True
    assert body["stats"]["pending"] == 0
    assert body["stats"]["created"] == 0


def test_suspend_then_approve_via_ops(client):
    suspend, gate_req = _suspend()
    sid = suspend.sidecar_request_id
    assert sid and not suspend.approved  # 手工挂起，桥不代批

    listing = client.get("/ops/hitl/approvals").json()
    assert len(listing["pending"]) == 1
    assert listing["pending"][0]["request_id"] == sid
    assert listing["pending"][0]["tool_name"] == "delete_records"
    assert listing["stats"]["created"] == 1
    assert listing["stats"]["pending"] == 1

    resp = client.post(f"/ops/hitl/approvals/{sid}", json={"action": "approve"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["approved"] is True
    assert data["result_action"] == "approve"
    assert data["gate_request_id"] == gate_req.request_id

    # 闸门已批准 ⇒ 同参数 preflight 放行（一次批准 = 一次执行由消费语义保证）
    gate = get_hitl_bridge().gate
    matched = gate.preflight("delete_records", {"table": "orders"},
                             declared_level=declared_level_for("L2"))
    assert matched is not None and matched.decision is ReviewDecision.APPROVED

    after = client.get("/ops/hitl/approvals").json()
    assert after["pending"] == []
    assert after["stats"]["approved"] == 1
    assert after["stats"]["pending"] == 0


def test_ops_reject_keeps_fail_closed(client):
    suspend, _ = _suspend()
    sid = suspend.sidecar_request_id

    resp = client.post(f"/ops/hitl/approvals/{sid}", json={"action": "reject"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["approved"] is False
    assert resp.json()["result_action"] == "reject"

    gate = get_hitl_bridge().gate
    with pytest.raises(ReviewRequired):  # 拒绝不放行：再次拦截
        gate.preflight("delete_records", {"table": "orders"},
                       declared_level=declared_level_for("L2"))
    assert client.get("/ops/hitl/approvals").json()["stats"]["rejected"] == 1


def test_ops_decide_unknown_id_returns_404(client):
    resp = client.post("/ops/hitl/approvals/nope", json={"action": "approve"})
    assert resp.status_code == 404
    assert get_hitl_bridge().stats()["not_found"] == 1


def test_ops_decide_invalid_action_returns_400(client):
    suspend, _ = _suspend()
    resp = client.post(
        f"/ops/hitl/approvals/{suspend.sidecar_request_id}",
        json={"action": "yolo"},
    )
    assert resp.status_code == 400
    assert "approve" in resp.json()["detail"]
    # 未落任何判定：仍可经合法裁决消费
    resp2 = client.post(
        f"/ops/hitl/approvals/{suspend.sidecar_request_id}",
        json={"action": "approve"},
    )
    assert resp2.status_code == 200 and resp2.json()["approved"] is True


def test_ops_edit_without_arguments_returns_400_and_keeps_pending(client):
    suspend, gate_req = _suspend()
    resp = client.post(
        f"/ops/hitl/approvals/{suspend.sidecar_request_id}",
        json={"action": "edit"},
    )
    assert resp.status_code == 400  # decision.error：EDIT 缺 edited_arguments
    assert get_hitl_bridge().gate.get(gate_req.request_id).decision is ReviewDecision.PENDING
    assert client.get("/ops/hitl/approvals").json()["stats"]["decided"] == 0


# ---------------------------------------------------------------------------
# 3. 死转移处置（PAUSED_FOR_APPROVAL → WAITING_APPROVAL 单轨）
# ---------------------------------------------------------------------------


def test_paused_for_approval_status_and_edges_removed():
    assert not hasattr(AgentStatus, "PAUSED_FOR_APPROVAL")
    assert "paused_for_approval" not in {s.value for s in AgentStatus}
    assert len(AgentStatus) == 11  # PROJECT_SPEC §10.2：11 个值


def test_approval_lane_roundtrip_single_status():
    state = AgentState(run_id="p64")
    LifecycleManager.transition(state, AgentStatus.RUNNING)
    LifecycleManager.transition(state, AgentStatus.EXECUTING)
    LifecycleManager.transition(state, AgentStatus.WAITING_APPROVAL)
    # 恢复面：WAITING_APPROVAL → RUNNING / EXECUTING 皆合法
    LifecycleManager.transition(state, AgentStatus.EXECUTING)
    LifecycleManager.transition(state, AgentStatus.WAITING_APPROVAL)
    LifecycleManager.transition(state, AgentStatus.RUNNING)
    assert state.status is AgentStatus.RUNNING
    for target in (
        AgentStatus.RUNNING,
        AgentStatus.EXECUTING,
        AgentStatus.FAILED,
        AgentStatus.CANCELLED,
    ):
        assert target in ALLOWED_TRANSITIONS[AgentStatus.WAITING_APPROVAL]


# ---------------------------------------------------------------------------
# 4. application/runtime 直删（0 importer，防回潮）
# ---------------------------------------------------------------------------


def test_application_runtime_directory_removed():
    assert not (_REPO_ROOT / "agentix" / "application" / "runtime").exists()


def test_application_runtime_import_fails():
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agentix.application.runtime")
