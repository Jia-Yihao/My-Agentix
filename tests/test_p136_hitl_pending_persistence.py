"""P136（T18）：HITL 审批 pending 落库（Q7 已裁决：Postgres 为真源）。

对应规划 `docs/design/审批与中断恢复优化规划.md` T18 卡（验收标准①–⑤）：

① 新建 pending 落库，裁决（approve/reject/edit/resume）更新**同库闭合**；
② 启动期 restore 把未决 pending 拉回内存可裁决（`install_executor_hook` 接线）；
③ 跨实例：内存里没有该 pending 的实例从库判定（**不再 404**）；
④ flag-off（默认）逐位等价：不建 store、不读写新表（桩 builder 零调用证明）、
   回执与 stats 无新增键；
⑤ 落库失败**显式可观测**：error 日志 + 回执 `persist_error` 标记 + stats 计数，
   绝不静默降级成纯内存还宣称成功。

口径（写进 store 模块注释的同一真源）：内存 FIFO 封顶 200 只是进程内存保护，
**库侧同样封顶未决**（200 行，超限抛 `pending_store_full`；已决（闭合）行不计入
未决上限，当前**不另设上限**——保留/清理策略属后续卡，本卡不冒充已有）——被挡门
外的最老未决 pending 行随带 `persist_error=pending_store_full` 回执与 error 日志
显式可见，绝不静默。

**不依赖真 Postgres**（本机无容器）：sqlite StaticPool 内存 engine + 注入
session_factory，与 `tests/test_p110_lazy_database_engine.py` 的「不建真连」
风格一致——默认连接面（共享惰性 engine）只验证**构造期不建池**。
真库上的重启/多副本行为用例属 T28（integration），本文件不冒充。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from loguru import logger
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agentix.config import settings
from agentix.infrastructure.persistence import hitl_pending_store as store_module
from agentix.infrastructure.persistence.hitl_pending_store import (
    DECIDED_STATUSES,
    HitlPendingStore,
    STATUS_PENDING,
    build_default_hitl_pending_store,
)
from agentix.policies.hitl import hitl_bridge as bridge_module
from agentix.policies.hitl.hitl_bridge import (
    HitlBridge,
    get_hitl_bridge,
    install_executor_hook,
    reset_hitl_bridge,
)
from agentix.policies.hitl.sidecar import ReviewDecisionAction, ReviewerSidecar
from agentix.tools.gateway.executor import get_tool_executor, reset_tool_executor
from agentix.tools.gateway.human_review import (
    HumanReviewGate,
    ReviewRequest,
    RiskLevel,
    mark_decision_surface,
    reset_review_gate,
)

#: flag-off 基线 stats 键集合（新增键只在 store 装配时出现——纪律 1 的判据之一）
BASELINE_STATS_KEYS = {
    "enabled", "mode", "created", "approved", "rejected", "edited", "resumed",
    "auto", "bypass", "not_found", "pending", "decided", "rejection_rate",
}

#: ops decide 基线响应键集合（persist_error 只在非空时出现）
BASELINE_DECIDE_BODY_KEYS = {
    "gate_request_id", "sidecar_request_id", "action", "result_action",
    "approved", "merged_arguments", "reason", "gate_recovered",
}


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


def _request(n: int) -> ReviewRequest:
    """直造一条被拦下的审批请求（无需注册工具，风险 HIGH 命中审批词）。"""
    return ReviewRequest(
        request_id=f"gate-{n}",
        tool_name="delete_records",
        risk_level=RiskLevel.HIGH,
        reason="t18 test suspend",
        args_digest={"table": "orders"},
    )


def _make_bridge(store=None) -> HitlBridge:
    return HitlBridge(
        gate=HumanReviewGate(enabled=True),
        sidecar=ReviewerSidecar(),
        pending_store=store,
    )


def _suspend(bridge: HitlBridge, n: int, *, thread_id: str = "t-1"):
    req = _request(n)
    suspend = asyncio.run(
        bridge.open_from_gate(req, {"table": "orders"}, thread_id=thread_id)
    )
    return req, suspend


@pytest.fixture()
def store() -> HitlPendingStore:
    """sqlite 内存 store（不触真 Postgres，本机无容器）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    target = HitlPendingStore(session_factory=sessionmaker(bind=engine, expire_on_commit=False))
    target.ensure_ready()
    return target


@pytest.fixture()
def error_logs():
    """捕获 loguru ERROR 级消息（验收⑤的可观测面）。"""
    records: list[str] = []
    handler_id = logger.add(lambda m: records.append(str(m)), level="ERROR")
    try:
        yield records
    finally:
        logger.remove(handler_id)


class _StubStore:
    """故障注入桩：save/close/load/list 任一环节可模拟抛错，并记录调用。"""

    def __init__(
        self,
        *,
        save_fail: bool = False,
        close_fail: bool = False,
        load_fail: bool = False,
        list_fail: bool = False,
        rows: dict | None = None,
    ) -> None:
        self._save_fail = save_fail
        self._close_fail = close_fail
        self._load_fail = load_fail
        self._list_fail = list_fail
        self.calls: list[tuple] = []
        self._rows = dict(rows or {})

    def ensure_ready(self) -> None:
        self.calls.append(("ensure_ready", {}))

    def save_pending(self, **kw) -> None:
        self.calls.append(("save_pending", kw))
        if self._save_fail:
            raise RuntimeError("simulated db write failure")
        sidecar = kw["sidecar"]
        self._rows[sidecar["request_id"]] = {
            "sidecar_request_id": sidecar["request_id"],
            "gate_request_id": kw.get("gate_request", {}).get("request_id", ""),
            "thread_id": kw.get("thread_id", ""),
            "status": STATUS_PENDING,
            "created_at": sidecar.get("created_at", time.time()),
            "decided_at": None,
            "decided_by": None,
            "decision_reason": None,
            "edited_arguments": None,
            "sidecar": dict(sidecar),
            "gate_request": dict(kw.get("gate_request") or {}),
        }

    def load(self, sidecar_request_id: str):
        self.calls.append(("load", (sidecar_request_id,)))
        if self._load_fail:
            raise RuntimeError("simulated db read failure")
        return self._rows.get(sidecar_request_id)

    def list_unresolved(self) -> list:
        self.calls.append(("list_unresolved", ()))
        if self._list_fail:
            raise RuntimeError("simulated db list failure")
        return [r for r in self._rows.values() if r["status"] == STATUS_PENDING]

    def close_decision(self, sidecar_request_id: str, **kw) -> None:
        self.calls.append(("close_decision", (sidecar_request_id, kw)))
        if self._close_fail:
            raise RuntimeError("simulated db close failure")
        row = self._rows.get(sidecar_request_id)
        if row is None:
            row = {"sidecar_request_id": sidecar_request_id, "status": None}
            self._rows[sidecar_request_id] = row
        row["status"] = kw["status"]
        row["decided_by"] = kw.get("decided_by")
        row["decision_reason"] = kw.get("reason")
        row["edited_arguments"] = kw.get("edited_arguments")
        row["decided_at"] = time.time()


# ---------------------------------------------------------------------------
# 1. 验收④：flag-off 逐位等价（纪律 1，reviewer 最硬核对项）
# ---------------------------------------------------------------------------


class TestFlagOffEquivalence:
    def test_default_is_off(self):
        """纪律 1：新 flag 一律 False 起步（静态判据，不依赖宿主 .env）。"""
        # 与 test_p41:940 同法：从类上取 model_fields，避开实例访问的 pydantic 2.11 弃用
        assert type(settings).model_fields["HITL_PENDING_PERSIST_ENABLED"].default is False

    def test_flag_off_builds_no_store_and_receipts_are_baseline(self, monkeypatch):
        """flag-off：store 工厂**被调用即失败**的桩证明零调用；回执/统计与基线逐键一致。"""
        calls: list[int] = []

        def _forbidden_builder():
            calls.append(1)
            raise AssertionError("flag-off 不得构造 pending store")

        monkeypatch.setattr(store_module, "build_default_hitl_pending_store", _forbidden_builder)
        monkeypatch.setattr(settings, "HITL_PENDING_PERSIST_ENABLED", False, raising=False)
        reset_hitl_bridge()
        try:
            bridge = get_hitl_bridge()
            assert bridge.pending_store is None
            assert calls == []  # 工厂零调用

            req, suspend = _suspend(bridge, 1)
            decision = asyncio.run(
                bridge.decide(suspend.sidecar_request_id, ReviewDecisionAction.APPROVE, by="ops")
            )
            assert suspend.persist_error == ""
            assert decision.persist_error == ""
            assert decision.approved is True
            stats = bridge.stats()
            assert set(stats) == BASELINE_STATS_KEYS  # 无新增键 = 观测面基线不变
        finally:
            reset_hitl_bridge()

    def test_flag_off_injected_none_store_never_touches_db(self, monkeypatch, error_logs):
        """store=None（桥直构）：挂起/裁决全程不触任何库调用。"""
        monkeypatch.setattr(settings, "HITL_PENDING_PERSIST_ENABLED", False, raising=False)
        bridge = _make_bridge(store=None)
        _, suspend = _suspend(bridge, 2)
        decision = asyncio.run(
            bridge.decide(suspend.sidecar_request_id, ReviewDecisionAction.REJECT, by="ops", reason="no")
        )
        assert decision.result_action is ReviewDecisionAction.REJECT
        assert suspend.persist_error == "" and decision.persist_error == ""
        assert set(bridge.stats()) == BASELINE_STATS_KEYS
        assert error_logs == []  # 基线路径无新增 error 噪音


# ---------------------------------------------------------------------------
# 2. 验收①：新建落库 + 裁决同库闭合（flag-on 语义面）
# ---------------------------------------------------------------------------


class TestPersistAndClose:
    def test_suspend_persists_pending_row(self, store):
        bridge = _make_bridge(store=store)
        req, suspend = _suspend(bridge, 3, thread_id="t-1")
        record = store.load(suspend.sidecar_request_id)
        assert record is not None
        assert record["status"] == STATUS_PENDING
        assert record["gate_request_id"] == req.request_id
        assert record["thread_id"] == "t-1"
        assert record["sidecar"]["tool_name"] == "delete_records"
        assert record["sidecar"]["arguments"] == {"table": "orders"}
        assert record["gate_request"]["request_id"] == req.request_id
        assert record["decided_at"] is None
        assert suspend.persist_error == ""

    def test_approve_closes_same_row(self, store):
        bridge = _make_bridge(store=store)
        _, suspend = _suspend(bridge, 4)
        decision = asyncio.run(
            bridge.decide(suspend.sidecar_request_id, ReviewDecisionAction.APPROVE, by="ops")
        )
        assert decision.approved is True and decision.persist_error == ""
        record = store.load(suspend.sidecar_request_id)
        assert record["status"] == "approved"
        assert record["decided_by"] == "ops"
        assert record["decided_at"] is not None

    def test_reject_and_edit_close_with_reason_and_merged(self, store):
        bridge = _make_bridge(store=store)
        _, s_reject = _suspend(bridge, 5)
        asyncio.run(
            bridge.decide(s_reject.sidecar_request_id, ReviewDecisionAction.REJECT, by="ops", reason="deny")
        )
        rec = store.load(s_reject.sidecar_request_id)
        assert rec["status"] == "rejected" and rec["decision_reason"] == "deny"

        _, s_edit = _suspend(bridge, 6)
        asyncio.run(
            bridge.decide(
                s_edit.sidecar_request_id,
                ReviewDecisionAction.EDIT,
                edited_arguments={"table": "orders_v2"},
                by="ops",
            )
        )
        rec = store.load(s_edit.sidecar_request_id)
        assert rec["status"] == "edited"
        # merged = 原参数 ∪ edits（与 BridgeDecision.merged_arguments 同源落库）
        assert rec["edited_arguments"] == {"table": "orders_v2"}

    def test_resume_closes_as_resumed(self, store):
        bridge = _make_bridge(store=store)
        _, suspend = _suspend(bridge, 7)
        asyncio.run(
            bridge.decide(suspend.sidecar_request_id, ReviewDecisionAction.RESUME, by="ops")
        )
        assert store.load(suspend.sidecar_request_id)["status"] == "resumed"

    def test_auto_bypass_paths_do_not_persist(self, store):
        """bypass 自动放行不是 pending，不落库（治理面不变，库面也不长孤儿行）。"""
        from agentix.policies.hitl.hitl_bridge import PermissionMode

        gate = HumanReviewGate(enabled=True)
        bridge = HitlBridge(gate=gate, sidecar=ReviewerSidecar(), pending_store=store,
                            permission_mode=PermissionMode.BYPASS)
        req = _request(8)
        gate._register(req)
        suspend = asyncio.run(bridge.open_from_gate(req, {"table": "orders"}))
        assert suspend.auto is True and suspend.sidecar_request_id == ""
        assert store.list_unresolved() == []


# ---------------------------------------------------------------------------
# 3. 验收②：启动期 restore 拉回未决 pending 并可裁决
# ---------------------------------------------------------------------------


class TestStartupRestore:
    def test_restore_pulls_back_and_decideable(self, store):
        """实例 A 挂起 → 新实例 B（同库）restore → 裁决成功且闭合。"""
        bridge_a = _make_bridge(store=store)
        _, suspend = _suspend(bridge_a, 9)

        bridge_b = _make_bridge(store=store)  # 全新内存（含新 gate）
        assert bridge_b.restore_pending() == 1
        listing = {p["request_id"] for p in bridge_b.pending()}
        assert suspend.sidecar_request_id in listing

        decision = asyncio.run(
            bridge_b.decide(suspend.sidecar_request_id, ReviewDecisionAction.APPROVE, by="ops")
        )
        assert decision.not_found is False and decision.approved is True
        assert store.load(suspend.sidecar_request_id)["status"] == "approved"

    def test_restore_skips_resolved_rows(self, store):
        bridge = _make_bridge(store=store)
        _, suspend = _suspend(bridge, 10)
        asyncio.run(bridge.decide(suspend.sidecar_request_id, ReviewDecisionAction.REJECT, by="ops"))
        fresh = _make_bridge(store=store)
        assert fresh.restore_pending() == 0
        assert fresh.pending() == []

    def test_install_executor_hook_restores_at_startup(self, monkeypatch, store):
        """生产接线面：install_executor_hook（main 启动期唯一入口）自带 restore。"""
        bridge_a = _make_bridge(store=store)
        _, suspend = _suspend(bridge_a, 11)

        monkeypatch.setattr(bridge_module, "_resolve_pending_store", lambda: store)
        reset_hitl_bridge()
        reset_tool_executor()
        try:
            bridge = install_executor_hook()
            assert get_tool_executor()._review_blocked_hook is not None
            listing = {p["request_id"] for p in bridge.pending()}
            assert suspend.sidecar_request_id in listing
        finally:
            get_tool_executor().set_review_blocked_hook(None)
            reset_tool_executor()
            reset_hitl_bridge()

    def test_restore_failure_is_observable_not_silent(self, monkeypatch, error_logs):
        """restore 读库失败：计数 restore_errors + error 日志，且不阻断接线（治理面仍 fail-closed）。"""
        failing = _StubStore(list_fail=True)
        monkeypatch.setattr(bridge_module, "_resolve_pending_store", lambda: failing)
        reset_hitl_bridge()
        reset_tool_executor()
        try:
            bridge = install_executor_hook()
            assert get_tool_executor()._review_blocked_hook is not None  # 接线不被打断
            assert bridge.stats()["restore_errors"] >= 1  # 失败可计数（运维看得见）
            assert any("restore" in m.lower() and "failed" in m.lower() for m in error_logs)
        finally:
            get_tool_executor().set_review_blocked_hook(None)
            reset_tool_executor()
            reset_hitl_bridge()


# ---------------------------------------------------------------------------
# 4. 验收③：跨实例从库判定（不再 404）
# ---------------------------------------------------------------------------


class TestCrossInstanceDecide:
    def test_decide_without_restore_loads_from_store(self, store):
        """实例 B 内存无此 pending、也没跑 restore → decide 直接从库判定。"""
        bridge_a = _make_bridge(store=store)
        _, suspend = _suspend(bridge_a, 12)

        bridge_b = _make_bridge(store=store)  # 未 restore
        decision = asyncio.run(
            bridge_b.decide(suspend.sidecar_request_id, ReviewDecisionAction.APPROVE, by="ops")
        )
        assert decision.not_found is False
        assert decision.approved is True
        # 闸门记录按库存载荷同式重建（_request_from_dict 通道），无需桥级
        # _rebuild_gate_request 兜底 ⇒ gate_recovered 保持 False（重建痕迹更少=更保真）
        assert decision.gate_recovered is False
        assert store.load(suspend.sidecar_request_id)["status"] == "approved"

    def test_pending_listing_includes_store_rows(self, store):
        """跨实例的 GET 待审面也认库（否则运维根本拿不到 pending id 去裁决）。"""
        bridge_a = _make_bridge(store=store)
        _, suspend = _suspend(bridge_a, 13)
        bridge_b = _make_bridge(store=store)
        listing = {p["request_id"] for p in bridge_b.pending()}
        assert suspend.sidecar_request_id in listing

    def test_truly_unknown_id_still_not_found(self, store):
        bridge = _make_bridge(store=store)
        decision = asyncio.run(bridge.decide("no-such-id", ReviewDecisionAction.APPROVE))
        assert decision.not_found is True

    def test_already_decided_row_is_not_redecided(self, store):
        """库行已闭合 ⇒ 不得二次裁决（fail-closed 语义不因落库而放宽）。"""
        bridge = _make_bridge(store=store)
        _, suspend = _suspend(bridge, 14)
        asyncio.run(bridge.decide(suspend.sidecar_request_id, ReviewDecisionAction.APPROVE, by="ops"))
        fresh = _make_bridge(store=store)
        decision = asyncio.run(
            fresh.decide(suspend.sidecar_request_id, ReviewDecisionAction.APPROVE, by="ops")
        )
        assert decision.not_found is True

    def test_store_load_failure_falls_back_to_not_found_fail_closed(self, error_logs):
        """读库故障 ⇒ 维持 not_found（404）这一 fail-closed 兜底 + error 日志，不谎判。"""
        failing = _StubStore(load_fail=True)
        bridge = _make_bridge(store=failing)
        decision = asyncio.run(bridge.decide("whatever-id", ReviewDecisionAction.APPROVE))
        assert decision.not_found is True
        assert any("load" in m.lower() or "restore" in m.lower() for m in error_logs)


# ---------------------------------------------------------------------------
# 5. 验收⑤：落库失败显式可观测（纪律 2 的边界：本任务按⑤执行，不吞）
# ---------------------------------------------------------------------------


class TestPersistFailureObservable:
    def test_save_failure_marks_suspend_receipt(self, error_logs):
        failing = _StubStore(save_fail=True)
        bridge = _make_bridge(store=failing)
        _, suspend = _suspend(bridge, 15)
        # 内存挂起仍成立（审批链不被阻断），但回执带可见标记 + 计数 + error 日志
        assert suspend.sidecar_request_id != ""
        assert "pending_persist_failed" in suspend.persist_error
        assert bridge.stats()["persist_errors"] == 1
        assert any("persist" in m.lower() and "failed" in m.lower() for m in error_logs)

    def test_close_failure_marks_decision_receipt(self, error_logs):
        failing = _StubStore(close_fail=True)
        bridge = _make_bridge(store=failing)
        _, suspend = _suspend(bridge, 16)  # save 正常
        decision = asyncio.run(
            bridge.decide(suspend.sidecar_request_id, ReviewDecisionAction.APPROVE, by="ops")
        )
        assert decision.approved is True  # 闸门已落批准（治理语义）
        # 但库未闭合 → 回执必须带标记（不谎报"完全成功"）
        assert "pending_close_failed" in decision.persist_error
        assert bridge.stats()["persist_errors"] == 1
        assert any("close" in m.lower() and "failed" in m.lower() for m in error_logs)

    def test_store_none_never_sets_markers(self):
        """flag-off（store=None）：即使同一条链路出错路径也不存在标记字段值——基线零变化。"""
        bridge = _make_bridge(store=None)
        _, suspend = _suspend(bridge, 17)
        decision = asyncio.run(
            bridge.decide(suspend.sidecar_request_id, ReviewDecisionAction.APPROVE, by="ops")
        )
        assert suspend.persist_error == "" and decision.persist_error == ""
        assert "persist_errors" not in bridge.stats() and "restore_errors" not in bridge.stats()


# ---------------------------------------------------------------------------
# 6. flag 接线面：get_hitl_bridge 按 settings 装配/不装配 store
# ---------------------------------------------------------------------------


class TestFlagWiring:
    def test_flag_on_singleton_gets_store(self, monkeypatch):
        stub = _StubStore()
        monkeypatch.setattr(store_module, "build_default_hitl_pending_store", lambda: stub)
        monkeypatch.setattr(settings, "HITL_PENDING_PERSIST_ENABLED", True, raising=False)
        reset_hitl_bridge()
        try:
            bridge = get_hitl_bridge()
            assert bridge.pending_store is stub
            assert get_hitl_bridge() is bridge  # 单例语义不变
            assert set(bridge.stats()) == BASELINE_STATS_KEYS | {"persist_errors", "restore_errors"}
        finally:
            reset_hitl_bridge()

    def test_default_builder_is_lazy_and_returns_store(self):
        """默认连接面：构造不触库不建池（§24.2 G10 惰性 engine 口径）。"""
        built = build_default_hitl_pending_store()
        assert isinstance(built, HitlPendingStore)
        assert built._session_factory is None  # 真会话工厂推迟到操作时刻解析


# ---------------------------------------------------------------------------
# 7. store/repo 单元语义（sqlite 内存，无真 Postgres）
# ---------------------------------------------------------------------------


class TestStoreSemantics:
    def test_ensure_ready_is_idempotent(self, store):
        store.ensure_ready()  # 第二次不得报错
        assert store.list_unresolved() == []

    def test_save_pending_is_idempotent(self, store):
        payload = {
            "request_id": "sb-1",
            "tool_name": "delete_records",
            "arguments": {"table": "orders"},
            "declared_risk_level": "L2",
            "evidence_ids": [],
            "artifact_refs": [],
            "created_at": time.time(),
        }
        store.save_pending(sidecar=payload, gate_request={"request_id": "g-1"}, thread_id="t")
        store.save_pending(sidecar=payload, gate_request={"request_id": "g-1"}, thread_id="t")
        assert len(store.list_unresolved()) == 1

    def test_close_decision_upserts_unseen_row(self, store):
        """库是裁决真源：裁决先于落库（历史 pending）也要闭合，不许"库里看不见就不算数"。"""
        store.close_decision(
            "sb-legacy",
            status="approved",
            decided_by="ops",
            reason="",
            edited_arguments=None,
            sidecar={
                "request_id": "sb-legacy",
                "tool_name": "delete_records",
                "arguments": {},
                "declared_risk_level": "L2",
                "evidence_ids": [],
                "artifact_refs": [],
                "created_at": time.time(),
            },
        )
        record = store.load("sb-legacy")
        assert record is not None
        assert record["status"] == "approved"

    def test_close_decision_rejects_unknown_status(self, store):
        with pytest.raises(ValueError):
            store.close_decision(
                "sb-x",
                status="exploded",  # 不在 {approved,rejected,edited,resumed} 值域
                decided_by="ops",
                reason="",
                edited_arguments=None,
                sidecar={"request_id": "sb-x", "tool_name": "t", "arguments": {},
                         "declared_risk_level": "L0", "evidence_ids": [],
                         "artifact_refs": [], "created_at": time.time()},
            )

    def test_status_vocabulary_matches_bridge_buckets(self):
        """口径同源：库状态词汇 = 桥裁决桶（纪律：同一概念一种叫法）。"""
        assert DECIDED_STATUSES == {"approved", "rejected", "edited", "resumed"}
        assert STATUS_PENDING == "pending"

    def test_list_unresolved_excludes_decided(self, store):
        bridge = _make_bridge(store=store)
        _, s1 = _suspend(bridge, 18)
        _, s2 = _suspend(bridge, 19)
        asyncio.run(bridge.decide(s1.sidecar_request_id, ReviewDecisionAction.REJECT, by="ops"))
        unresolved = {r["sidecar_request_id"] for r in store.list_unresolved()}
        assert unresolved == {s2.sidecar_request_id}


# ---------------------------------------------------------------------------
# 8. 溢出策略：库侧同样封顶 200（真源口径写死，不留含糊）
# ---------------------------------------------------------------------------


class TestStoreCapPolicy:
    def test_store_caps_unresolved_at_200_with_visible_marker(self, store):
        """未决第 201 条：拒绝写入 + persist_error 标记（绝不静默挤掉真源行）。"""
        bridge = _make_bridge(store=store)
        for i in range(store.max_unresolved):
            _, susp = _suspend(bridge, 100 + i)
            assert susp.persist_error == ""
        _, overflow = _suspend(bridge, 999)
        assert "pending_store_full" in overflow.persist_error
        assert store.load(overflow.sidecar_request_id) is None
        assert len(store.list_unresolved()) == store.max_unresolved

    def test_cap_counts_only_unresolved(self, store):
        """闭合即腾位：已决行不计入未决上限（否则长期运行后必然溢出）。"""
        bridge = _make_bridge(store=store)
        sid_resolved = []
        for i in range(5):
            _, susp = _suspend(bridge, 200 + i)
            sid_resolved.append(susp.sidecar_request_id)
        for sid in sid_resolved:
            asyncio.run(bridge.decide(sid, ReviewDecisionAction.APPROVE, by="ops"))
        _, susp = _suspend(bridge, 205)
        assert susp.persist_error == ""  # 20 未决（15 挂起 + 之前 5 条已移出未决集合）内不溢出
        assert store.load(susp.sidecar_request_id) is not None


# ---------------------------------------------------------------------------
# 9. ops 面（TestClient，p64 同范式）：跨实例不再 404 + 标记键只在故障时出现
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from agentix.infrastructure.core.security import verify_api_key
    from agentix.interfaces.http import ops_router

    app = FastAPI()
    app.include_router(ops_router.router)
    app.dependency_overrides[verify_api_key] = lambda: None
    with TestClient(app) as test_client:
        yield test_client


class TestOpsSurface:
    def _fresh_singleton(self, monkeypatch, store_obj):
        monkeypatch.setattr(bridge_module, "_resolve_pending_store", lambda: store_obj)
        monkeypatch.setattr(settings, "HITL_PENDING_PERSIST_ENABLED", True, raising=False)
        reset_hitl_bridge()
        reset_review_gate()
        mark_decision_surface()

    def test_cross_instance_decide_returns_200_not_404(self, monkeypatch, client, store):
        """实例 A 落库 → 实例 B（新单例）ops decide：200 且闭合（不再 404）。"""
        bridge_a = _make_bridge(store=store)
        _, suspend = _suspend(bridge_a, 21)

        self._fresh_singleton(monkeypatch, store)
        try:
            listing = client.get("/ops/hitl/approvals").json()
            assert suspend.sidecar_request_id in {p["request_id"] for p in listing["pending"]}
            resp = client.post(
                f"/ops/hitl/approvals/{suspend.sidecar_request_id}",
                json={"action": "approve", "by": "ops"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["approved"] is True
            assert "persist_error" not in body  # 成功路径键集合与基线一致
            assert set(body) == BASELINE_DECIDE_BODY_KEYS
            assert store.load(suspend.sidecar_request_id)["status"] == "approved"
        finally:
            reset_hitl_bridge()
            reset_review_gate()

    def test_persist_failure_surfaces_persist_error_key(self, monkeypatch, client, error_logs):
        """落库失败经 ops 回执对运维可见（验收⑤的 API 面）；flag-off 成功路径不带该键。"""
        failing = _StubStore(save_fail=True)
        self._fresh_singleton(monkeypatch, failing)
        try:
            bridge = get_hitl_bridge()
            _, suspend = _suspend(bridge, 22)
            assert "pending_persist_failed" in suspend.persist_error
            # stats 面同样可计数（ops GET 的观测面带 persist_errors）
            assert client.get("/ops/hitl/approvals").json()["stats"]["persist_errors"] >= 1
            # decide 正常闭合（close 桩成功），回执不带 persist_error——标记只随故障出现
            resp = client.post(
                f"/ops/hitl/approvals/{suspend.sidecar_request_id}",
                json={"action": "approve", "by": "ops"},
            )
            assert resp.status_code == 200
            assert "persist_error" not in resp.json()
        finally:
            reset_hitl_bridge()
            reset_review_gate()

    def test_flag_off_ops_body_keys_are_baseline(self, monkeypatch, client):
        """flag-off：decide 响应键集合与基线逐键一致（纪律 1 的 API 面证据）。"""
        monkeypatch.setattr(bridge_module, "_resolve_pending_store", lambda: None)
        monkeypatch.setattr(settings, "HITL_PENDING_PERSIST_ENABLED", False, raising=False)
        reset_hitl_bridge()
        reset_review_gate()
        mark_decision_surface()
        try:
            bridge = get_hitl_bridge()
            assert bridge.pending_store is None
            _, suspend = _suspend(bridge, 23)
            resp = client.post(
                f"/ops/hitl/approvals/{suspend.sidecar_request_id}",
                json={"action": "approve", "by": "ops"},
            )
            assert resp.status_code == 200
            assert set(resp.json()) == BASELINE_DECIDE_BODY_KEYS
        finally:
            reset_hitl_bridge()
            reset_review_gate()
