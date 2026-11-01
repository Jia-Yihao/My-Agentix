"""P165（T19）：审批等待 deadline，到期自动拒绝。

对应规划 `docs/design/审批与中断恢复优化规划.md` T19 卡（Q9 已裁决：到期自动拒绝）
与本卡验收标准①–④：

① `HITL_APPROVAL_DEADLINE_S` **缺省 = 现行为**（无独立 deadline，纪律 1）：
   默认 0 时**不建扫描任务、不读库**，`stats()` 键集合与基线逐字相同；
② 配置后到期自动拒绝，且归因为「超时拒」，与「人工拒」在**可查询字段**上可区分：
   `BridgeDecision.reason == "auto_reject_timeout"`、库行 `decided_by ==
   "system:deadline"` 与 `decision_reason == "auto_reject_timeout"`、
   `stats()["auto_reject_timeout"]`（指标词汇不动，仍记 `decision="rejected"`）；
③ deadline 与 `AGENT_TIMEOUT` 的先后关系有文档口径（守卫在
   `docs/architecture/运行时护栏.md` §8，本文件的用例覆盖"先到者生效"里本卡能做到的
   那一半：到期拒由扫描驱动、与图执行的外层墙钟互不依赖）；
④ 治理层终局性质不变：到期拒 = `gate.reject` 落地（fail-closed），不恢复图、不重开
   执行——本卡的挂起车道本来就没让图停住（hook 超时后 executor 已返回 blocked）。

覆盖面（逐项对应分派单 E1–E10）：

1. 配置默认 `0.0`、`ge=0` 校验；
2. `HitlPendingStore.list_expired`：只取 pending、`created_at <= cutoff`（含等号）、
   按挂起时刻升序、`limit` 生效；
3. `expire_overdue`：到期行闭合为 `rejected` + 归因字段、未到期行不动、
   `BridgeDecision.reason` 可区分、**闸门 reject 真被调用**；
4. 人工拒与超时拒可区分（同批两条 pending 各走一条路）；
5. deadline=0 或 store=None：返回 `[]`、`stats()` 键集合与基线逐字相同；
6. 单行失败不阻断其余；读库失败 fail-open（桩抛错 ⇒ `[]` + warning）；
7. `_hitl_sweep_interval` 三个边界（小/中/大 deadline）；
8. `_start_hitl_deadline_sweeper` 四条按序判定：deadline 关不建任务；`HUMAN_REVIEW`
   关 ⇒ warning + 不建；持久化关 ⇒ warning + 不建；桥缺失（hook 安装失败）⇒
   warning + 不建；齐备 ⇒ 建任务且可取消；
9. `_shutdown` 会取消扫描任务（假任务验证 + 句柄置回 None）；
10. 扫描循环：`expire_overdue` 抛错后循环继续（observation 计数 ≥ 2）；
11. 子集计数口径（T19 第 1 轮返工）：`rejected` 含超时拒、`auto_reject_timeout`
    只是子集且**不进** `decided` 求和，`rejection_rate` 不被稀释；
12. 指标面可区分（一般项 4）：到期拒打一次 `record_approval_timeout("tool")`，
    未落地（`not_found`）时**不打**；
13. 验收③：`AGENT_TIMEOUT` 取值不影响 pending 到期判定（两把尺子量纲不同）；
14. 单一配置读取点：`resolve_approval_deadline_s` 是唯一读取函数，桥单例与
    `main` 的扫描任务都调它；
15. 扫描租约（第 2 轮返工）：同一时刻只有一个扫描者、到期可被其他 worker 接管、
    同一 owner 可续租、释放后立即可取（多 worker 部署 `--workers 2` 的并发前提）；
16. 行认领：同一条 pending 只被一个扫描者裁决——同刻其他 owner 抢不到、同一 owner
    在 TTL 内也不许重复认领、已决行不可认领；
17. 并发端到端：已被其他 worker 认领的到期行不被本扫描者裁决（`rejected` 与
    `auto_reject_timeout` 都不动），拿不到租约的一轮**连库都不读**；
18. close 失败不重复计数（审查点名的最小用例）：认领保留 ⇒ 下一轮不重算；认领
    TTL 到期后按闸门**既有裁决**对账补闭合，统计与超时指标都不变；
19. 归因只对拒绝有效（`decide(APPROVE, attribution=...)` 抛 `ValueError`）；
    闸门已决时 `require_gate_pending=True` 的拒绝报 `gate_request_not_pending`
    且不计数、不闭合库行；
20. 关停释放扫描租约（句柄存桥，不重新装配单例）；`AGENT_TIMEOUT < deadline`
    的"先到者"形态下到期拒仍照常落地；
21. 跨实例裁决（第 4 轮返工，严重项 1）：对端实例把行拒掉后，本实例待审面自动剔除
    该行、在本实例再裁决返回 `not_found`，且库行的 `decided_by` / `decision_reason`
    / `status` 不被改写（超时归因不被一次人工点按抹掉）；
22. 闸门记录被 FIFO 200 逐出（第 4 轮返工，严重项 2）：按 sidecar 载荷补建闸门记录后
    再拒，库行闭合、`rejected` / `auto_reject_timeout` 各 +1、超时指标恰好一次
    （不再每轮 `did_not_land` 空转、永久占未决槽位）；
23. `close_decision` 已决行不覆盖（store 级 CAS，第 4 轮返工）：第二次裁决返回
    `False`，一个闭合列都不改；
24. `decide` 核库行 status 读失败 ⇒ fail-closed（`reason="pending_status_unreadable"`，
    `not_found=True`），不计入裁决桶（`not_found` 计数 +1）、库行仍 pending。

**不依赖真 Postgres**（本机无容器）：sqlite StaticPool 内存 engine + 注入
`session_factory`，与 `tests/test_p136_hitl_pending_persistence.py` 同款夹具。
既有测试文件（`test_p134` / `test_p136` / `test_agent_phase16_hitl_bridge`）只读不改。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, Dict, Iterable, List, Optional

import pytest
from loguru import logger
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agentix.config.settings import Settings, settings
from agentix.infrastructure.persistence.db.models.hitl_pending import HitlPendingApproval
from agentix.infrastructure.persistence.hitl_pending_store import HitlPendingStore
from agentix.policies.hitl.hitl_bridge import (
    AUTO_REJECT_TIMEOUT_ACTOR,
    AUTO_REJECT_TIMEOUT_REASON,
    HitlBridge,
    get_hitl_bridge,
    reset_hitl_bridge,
)
from agentix.policies.hitl.sidecar import ReviewDecisionAction, ReviewerSidecar
from agentix.tools.gateway.human_review import (
    HumanReviewGate,
    ReviewDecision,
    ReviewRequest,
    RiskLevel,
)

#: flag-off 基线 stats 键集合。照抄 `test_p136_hitl_pending_persistence.py:63` 的
#: 断言方式（同一份字面量，逐字相同）——纪律 1 的判据就是"键集合不变"。
BASELINE_STATS_KEYS = {
    "enabled", "mode", "created", "approved", "rejected", "edited", "resumed",
    "auto", "bypass", "not_found", "pending", "decided", "rejection_rate",
}

#: 装配了库面（store is not None）时多出来的故障计数键（T18 已定口径）
STORE_STATS_KEYS = BASELINE_STATS_KEYS | {"persist_errors", "restore_errors"}


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


def _request(n: int) -> ReviewRequest:
    """直造一条被拦下的审批请求（风险 HIGH 命中审批词，无需注册工具）。"""
    return ReviewRequest(
        request_id=f"gate-{n}",
        tool_name="delete_records",
        risk_level=RiskLevel.HIGH,
        reason="t19 test suspend",
        args_digest={"table": "orders"},
    )


def _make_bridge(store=None, *, deadline: float = 0.0) -> HitlBridge:
    return HitlBridge(
        gate=HumanReviewGate(enabled=True),
        sidecar=ReviewerSidecar(),
        pending_store=store,
        approval_deadline_s=deadline,
    )


class _Harness:
    """sqlite 内存 store + 直改 `created_at` 的后门（造"已到期"行用）。

    `open_from_gate` 的时间戳来自 `time.time()`，测试无法把"两小时前挂起"造出来；
    这里只在**测试侧**回写 `created_at`，不改生产可写面。
    """

    def __init__(self, store: HitlPendingStore, factory: sessionmaker) -> None:
        self.store = store
        self._factory = factory

    def set_created_at(self, sidecar_request_id: str, created_at: float) -> None:
        with self._factory() as session:
            row = session.get(HitlPendingApproval, sidecar_request_id)
            assert row is not None, f"pending row {sidecar_request_id} not found"
            row.created_at = float(created_at)
            session.commit()

    def expire_claim(self, sidecar_request_id: str) -> None:
        """把认领租约置为到期（模拟 TTL 已过，不必真等 120 秒）。

        刻意只动 `claim_until` 不动 `claim_owner`：认领到期的语义正是"谁都能接管"，
        与 owner 是谁无关——这也顺便证明认领判据确实按**时刻**而不是按 owner 比较。
        """
        with self._factory() as session:
            row = session.get(HitlPendingApproval, sidecar_request_id)
            assert row is not None, f"pending row {sidecar_request_id} not found"
            row.claim_until = 0.0
            session.commit()

    def snapshot(self, sidecar_request_id: str) -> Optional[Dict[str, Any]]:
        with self._factory() as session:
            row = session.get(HitlPendingApproval, sidecar_request_id)
            if row is None:
                return None
            return {
                "status": row.status,
                "decided_by": row.decided_by,
                "decision_reason": row.decision_reason,
                "created_at": row.created_at,
                "decided_at": row.decided_at,
                "claim_owner": row.claim_owner,
                "claim_until": row.claim_until,
            }


def _new_harness() -> _Harness:
    """造一台独立的内存库面（两条互不干扰的 store：验"量纲无关"要各扫一次）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    store = HitlPendingStore(session_factory=factory)
    store.ensure_ready()
    return _Harness(store, factory)


@pytest.fixture()
def harness() -> _Harness:
    return _new_harness()


@pytest.fixture()
def warning_logs():
    """捕获 loguru WARNING+ 级消息（fail-open / 配置无效的可观测面）。"""
    records: List[str] = []
    handler_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
    try:
        yield records
    finally:
        logger.remove(handler_id)


async def _suspend(bridge: HitlBridge, n: int) -> str:
    """经 executor hook 车道挂起一条 pending，返回 sidecar request id。

    末尾补一步 `restore_request`：生产里**闸门记录由执行点 preflight 建好**，
    `open_from_gate` 只是拦下之后的钩子（它不负责登记）。测试直接造的
    `ReviewRequest` 若不进闸门，`gate.reject` 就会因"闸门里没有这条"返回 False——
    那是夹具与生产形状不符，不是被测行为。
    """
    request = _request(n)
    suspend = await bridge.open_from_gate(request, {"table": "orders"}, thread_id="t-1")
    assert suspend.sidecar_request_id, "挂起必须拿到 sidecar request id"
    assert bridge.gate.restore_request(request), "闸门记录必须先于裁决存在（生产同形）"
    return suspend.sidecar_request_id


class _StubStore:
    """故障注入桩：只实现 T19 用到的库面方法（鸭子型 + 调用留痕）。

    ``lease_denied`` 模拟"另一个 worker 正持有扫描租约"；``claim_fail`` 模拟
    "这一行已被别的扫描者认领（或已决）"。两个开关都只在**桩侧**生效，
    真实并发语义由 sqlite/Postgres 上的条件更新承载。

    第 4 轮返工补齐两处：``statuses_for``（`pending()` 的库行 status 过滤）与
    ``load_fail``（`decide` 内存命中时核库行的读失败路径）。
    """

    def __init__(
        self,
        *,
        rows: Optional[List[Dict[str, Any]]] = None,
        read_fail: bool = False,
        lease_denied: bool = False,
        claim_fail: bool = False,
        load_fail: bool = False,
    ) -> None:
        self._rows = list(rows or [])
        self._read_fail = read_fail
        self._lease_denied = lease_denied
        self._claim_fail = claim_fail
        self._load_fail = load_fail
        self.calls: List[tuple] = []

    def acquire_sweep_lease(self, owner: str, **kw: Any) -> bool:
        self.calls.append(("acquire_sweep_lease", (owner, kw)))
        return not self._lease_denied

    def release_sweep_lease(self, owner: str) -> bool:
        self.calls.append(("release_sweep_lease", (owner,)))
        return True

    def claim_expired(self, sidecar_request_id: str, **kw: Any) -> bool:
        self.calls.append(("claim_expired", (sidecar_request_id, kw)))
        return not self._claim_fail

    def release_claim(self, sidecar_request_id: str, **kw: Any) -> bool:
        self.calls.append(("release_claim", (sidecar_request_id, kw)))
        return True

    def list_expired(self, cutoff_epoch: float, *, limit: int = 100) -> List[Dict[str, Any]]:
        self.calls.append(("list_expired", (cutoff_epoch, limit)))
        if self._read_fail:
            raise RuntimeError("simulated db read failure")
        return list(self._rows)

    def list_unresolved(self) -> List[Dict[str, Any]]:
        self.calls.append(("list_unresolved", ()))
        return [r for r in self._rows if r.get("status") == "pending"]

    def statuses_for(self, sidecar_request_ids: Iterable[str]) -> Dict[str, str]:
        """待审面过滤用：桩里默认「内存面列出的都是 pending」。"""
        self.calls.append(("statuses_for", (tuple(sidecar_request_ids),)))
        return {sid: "pending" for sid in sidecar_request_ids}

    def close_decision(self, sidecar_request_id: str, **kw: Any) -> bool:
        self.calls.append(("close_decision", (sidecar_request_id, kw)))
        # 桩语义与真实现一致：CAS 成功返回 ``True``。缺省路径必须表现为
        # "落库成功"，否则桥侧把 ``closed is True`` 当对账成功的前置判据时会失真。
        return True

    def load(self, sidecar_request_id: str) -> Optional[Dict[str, Any]]:
        self.calls.append(("load", (sidecar_request_id,)))
        if self._load_fail:
            raise RuntimeError("simulated db read failure")
        for row in self._rows:
            if row.get("sidecar_request_id") == sidecar_request_id:
                return row
        return None


# ---------------------------------------------------------------------------
# 1. 配置面（验收①：默认 = 现行为）
# ---------------------------------------------------------------------------


class TestConfigSurface:
    def test_default_is_zero(self):
        """纪律 1：新键默认关（0 = 不建任务、不读库、键集合不变）。"""
        assert Settings.model_fields["HITL_APPROVAL_DEADLINE_S"].default == 0.0

    @pytest.mark.parametrize("value", [-1, -0.5, -86400])
    def test_negative_deadline_rejected(self, value):
        with pytest.raises(ValidationError) as ei:
            Settings(_env_file=None, SECRET_KEY="x", DEBUG=True, HITL_APPROVAL_DEADLINE_S=value)
        assert "HITL_APPROVAL_DEADLINE_S" in str(ei.value)

    def test_positive_deadline_accepted(self):
        s = Settings(_env_file=None, SECRET_KEY="x", DEBUG=True, HITL_APPROVAL_DEADLINE_S=90)
        assert s.HITL_APPROVAL_DEADLINE_S == 90.0

    def test_reason_and_actor_literals_are_single_source(self):
        """归因字面量只有一个源：三处可见字段共用同一常量（改一处即全改）。"""
        assert AUTO_REJECT_TIMEOUT_REASON == "auto_reject_timeout"
        assert AUTO_REJECT_TIMEOUT_ACTOR == "system:deadline"


# ---------------------------------------------------------------------------
# 2. store 面：list_expired（验收②的取数口径）
# ---------------------------------------------------------------------------


class TestListExpired:
    def _row(self, n: int, created_at: float, *, status: str = "pending") -> Dict[str, Any]:
        return {
            "sidecar_request_id": f"sidecar-{n}",
            "gate_request_id": f"gate-{n}",
            "thread_id": "t-1",
            "status": status,
            "created_at": created_at,
            "decided_at": None,
            "decided_by": None,
            "decision_reason": None,
            "edited_arguments": None,
            "sidecar": {
                "request_id": f"sidecar-{n}",
                "tool_name": "delete_records",
                "arguments": {"table": "orders"},
                "declared_risk_level": "L2",
                "evidence_ids": [],
                "artifact_refs": [],
                "created_at": created_at,
            },
            "gate_request": None,
        }

    def _fill(self, store: HitlPendingStore) -> None:
        store.save_pending(sidecar=self._row(1, 100.0)["sidecar"], thread_id="t-1")
        store.save_pending(sidecar=self._row(2, 50.0)["sidecar"], thread_id="t-1")
        store.save_pending(sidecar=self._row(3, 900.0)["sidecar"], thread_id="t-1")
        store.save_pending(sidecar=self._row(4, 10.0)["sidecar"], thread_id="t-1")
        # 已决行：即便 created_at 很老也不得被扫到（重复裁决会破 fail-closed）
        store.save_pending(sidecar=self._row(5, 10.0)["sidecar"], thread_id="t-1")
        store.close_decision(
            "sidecar-5", status="approved", decided_by="ops", reason="ok",
            sidecar=self._row(5, 10.0)["sidecar"],
        )

    def test_only_pending_and_ordered_ascending(self, harness):
        self._fill(harness.store)
        rows = harness.store.list_expired(500.0)
        assert [r["sidecar_request_id"] for r in rows] == [
            "sidecar-4",
            "sidecar-2",
            "sidecar-1",
        ]
        assert all(r["status"] == "pending" for r in rows)

    def test_cutoff_is_inclusive(self, harness):
        self._fill(harness.store)
        assert [r["sidecar_request_id"] for r in harness.store.list_expired(100.0)] == [
            "sidecar-4",
            "sidecar-2",
            "sidecar-1",
        ]

    def test_limit_caps_one_sweep(self, harness):
        self._fill(harness.store)
        rows = harness.store.list_expired(1000.0, limit=2)
        assert [r["sidecar_request_id"] for r in rows] == ["sidecar-4", "sidecar-2"]

    def test_empty_when_nothing_expired(self, harness):
        self._fill(harness.store)
        assert harness.store.list_expired(9.0) == []


# ---------------------------------------------------------------------------
# 2.1 扫描租约（第 2 轮返工）：同一时刻只有一个扫描者
# ---------------------------------------------------------------------------


class TestSweepLease:
    """`--workers 2` 下每个进程都起扫描任务，"单扫描者"必须由库租约保证。

    全部用例注入 `now`：租约判据是**时刻比较**，靠真时钟会让断言在 CI 上抖动。
    """

    def test_single_holder_until_expiry_then_takeover(self, harness):
        store = harness.store
        assert store.acquire_sweep_lease("w1", now=100, ttl_s=10) is True, "首启 INSERT 即持有"
        assert store.acquire_sweep_lease("w2", now=100) is False, "别人持有且未到期"
        assert store.acquire_sweep_lease("w2", now=109) is False, "未到期前抢不到"
        assert store.acquire_sweep_lease("w2", now=111) is True, "到期 ⇒ 接管（持有者崩溃的自愈路径）"

    def test_same_owner_can_renew(self, harness):
        store = harness.store
        assert store.acquire_sweep_lease("w1", now=100, ttl_s=10) is True
        assert store.acquire_sweep_lease("w1", now=105, ttl_s=10) is True, "同一 owner 可续租"
        assert store.acquire_sweep_lease("w2", now=111) is False, "续租把到期推到 115"
        assert store.acquire_sweep_lease("w2", now=116) is True

    def test_release_frees_the_lease_immediately(self, harness):
        store = harness.store
        assert store.acquire_sweep_lease("w1", now=100, ttl_s=10) is True
        assert store.release_sweep_lease("w2") is False, "不是持有者就删不掉（否则慢进程会踢掉唯一扫描者）"
        assert store.release_sweep_lease("w1") is True
        assert store.acquire_sweep_lease("w2", now=100) is True, "释放后立即可取（行被重建）"


# ---------------------------------------------------------------------------
# 2.2 行认领（第 2 轮返工）：同一条 pending 只被一个扫描者裁决
# ---------------------------------------------------------------------------


class TestRowClaim:
    """租约只降低"两个扫描者"的概率，行认领才是"只裁决一次"的正确性兜底。"""

    @staticmethod
    def _sidecar(sid: str = "sidecar-1", *, created_at: float = 100.0) -> Dict[str, Any]:
        return {
            "request_id": sid,
            "tool_name": "delete_records",
            "arguments": {"table": "orders"},
            "declared_risk_level": "L2",
            "evidence_ids": [],
            "artifact_refs": [],
            "created_at": created_at,
        }

    def test_one_claim_per_row(self, harness):
        store = harness.store
        store.save_pending(sidecar=self._sidecar(), thread_id="t-1")

        assert store.claim_expired("sidecar-1", cutoff_epoch=200, owner="w1", now=100) is True
        assert store.claim_expired("sidecar-1", cutoff_epoch=200, owner="w2", now=100) is False
        assert store.claim_expired("sidecar-1", cutoff_epoch=200, owner="w1", now=105) is False, (
            "同一 owner 也不许在 TTL 内重复认领——close 失败的行若下一轮立即重算，就是重复计数"
        )
        assert store.release_claim("sidecar-1", owner="w1") is True
        assert store.claim_expired("sidecar-1", cutoff_epoch=200, owner="w2", now=105) is True

    def test_claim_respects_cutoff_and_ids(self, harness):
        store = harness.store
        store.save_pending(sidecar=self._sidecar(created_at=100.0), thread_id="t-1")
        assert store.claim_expired("sidecar-1", cutoff_epoch=50, owner="w1", now=100) is False, (
            "未到期的行不在本轮扫描范围内"
        )
        with pytest.raises(ValueError):
            store.claim_expired("", cutoff_epoch=200, owner="w1", now=100)
        with pytest.raises(ValueError):
            store.claim_expired("sidecar-1", cutoff_epoch=200, owner="", now=100)

    def test_closed_row_cannot_be_claimed(self, harness):
        store = harness.store
        store.save_pending(sidecar=self._sidecar(), thread_id="t-1")
        store.close_decision("sidecar-1", status="rejected", decided_by="ops", reason="manual")

        assert store.claim_expired("sidecar-1", cutoff_epoch=200, owner="w1", now=100) is False
        assert store.release_claim("sidecar-1", owner="w1") is False

    def test_close_and_resave_both_clear_the_claim(self, harness):
        store = harness.store
        store.save_pending(sidecar=self._sidecar(), thread_id="t-1")
        assert store.claim_expired("sidecar-1", cutoff_epoch=200, owner="w1", now=100) is True
        assert harness.snapshot("sidecar-1")["claim_owner"] == "w1"

        store.close_decision("sidecar-1", status="rejected", decided_by="ops", reason="manual")
        closed = harness.snapshot("sidecar-1")
        assert (closed["claim_owner"], closed["claim_until"]) == ("", 0.0), (
            "闭合 = 这一轮认领结束"
        )

        store.save_pending(sidecar=self._sidecar(created_at=300.0), thread_id="t-1")
        resaved = harness.snapshot("sidecar-1")
        assert (resaved["claim_owner"], resaved["claim_until"]) == ("", 0.0), (
            "重新挂起 = 新生命周期，旧认领不得留着占位"
        )


# ---------------------------------------------------------------------------
# 2.3 已决行不覆盖（第 4 轮返工，store 级 CAS）：裁决只写一次
# ---------------------------------------------------------------------------


class TestCloseDecisionCas:
    """跨实例竞态下，后手不得改写先手的归因（超时拒不能被人工裁决抹掉）。"""

    def test_close_decision_does_not_overwrite_decided_row(self, harness):
        store = harness.store
        sidecar = {
            "request_id": "sidecar-cas",
            "tool_name": "delete_records",
            "arguments": {"table": "orders"},
            "declared_risk_level": "L2",
            "evidence_ids": [],
            "artifact_refs": [],
            "created_at": 100.0,
        }
        store.save_pending(sidecar=sidecar, thread_id="t-1")

        assert store.close_decision(
            "sidecar-cas", status="rejected", decided_by="ops", reason="manual"
        ) is True, "未决行照旧可闭合（补写路径与 pending 行同一条）"

        # 第二次（对端实例 / 迟到的人工裁决）必须"没写成"，而不是改写归因
        assert store.close_decision(
            "sidecar-cas", status="approved", decided_by="human", reason="late approve"
        ) is False

        row = harness.snapshot("sidecar-cas")
        assert row["status"] == "rejected", "已超时拒绝的事实不许被点成 approved"
        assert row["decided_by"] == "ops", "先手的裁决人不许被后手覆盖"
        assert row["decision_reason"] == "manual", "先手的归因不许被后手覆盖"


# ---------------------------------------------------------------------------
# 3. expire_overdue：到期拒落地（验收②④）
# ---------------------------------------------------------------------------


class TestExpireOverdue:
    @pytest.mark.asyncio
    async def test_expired_pending_is_rejected_with_timeout_attribution(self, harness):
        bridge = _make_bridge(harness.store, deadline=60.0)
        expired_sid = await _suspend(bridge, 1)
        fresh_sid = await _suspend(bridge, 2)
        harness.set_created_at(expired_sid, time.time() - 600)
        harness.set_created_at(fresh_sid, time.time())

        decisions = await bridge.expire_overdue(60.0)

        assert [d.sidecar_request_id for d in decisions] == [expired_sid]
        decision = decisions[0]
        assert decision.result_action is ReviewDecisionAction.REJECT
        assert decision.approved is False
        assert decision.reason == AUTO_REJECT_TIMEOUT_REASON
        assert decision.not_found is False and decision.error == ""
        assert decision.persist_error == ""

        expired_row = harness.snapshot(expired_sid)
        assert expired_row["status"] == "rejected"
        assert expired_row["decided_by"] == AUTO_REJECT_TIMEOUT_ACTOR
        assert expired_row["decision_reason"] == AUTO_REJECT_TIMEOUT_REASON
        assert expired_row["decided_at"] is not None

        # 未到期行一个字段都不许动
        fresh_row = harness.snapshot(fresh_sid)
        assert fresh_row["status"] == "pending"
        assert fresh_row["decided_by"] is None
        assert fresh_row["decision_reason"] is None

    @pytest.mark.asyncio
    async def test_gate_reject_actually_landed(self, harness):
        """闸门 reject 真被调用：记录转为 REJECTED 且 note 带归因字面量。

        用第二台桥（内存空、闸门新）扫描 —— 这样闸门记录是**从库行回捞重建**的，
        与跨实例裁决同一条路径（本实例内存里已有该 pending 时不会重建闸门记录）。
        """
        producer = _make_bridge(harness.store, deadline=60.0)
        sid = await _suspend(producer, 1)
        harness.set_created_at(sid, time.time() - 600)

        gate = HumanReviewGate(enabled=True)
        sweeper = HitlBridge(
            gate=gate,
            sidecar=ReviewerSidecar(),
            pending_store=harness.store,
            approval_deadline_s=60.0,
        )
        decisions = await sweeper.expire_overdue(60.0)

        assert [d.sidecar_request_id for d in decisions] == [sid]
        record = gate.get("gate-1")
        assert record is not None, "到期拒前必须先把库行回捞成闸门记录"
        assert record.decision is ReviewDecision.REJECTED
        assert record.decided_by == AUTO_REJECT_TIMEOUT_ACTOR
        assert record.note == AUTO_REJECT_TIMEOUT_REASON
        assert gate.pending() == []

    @pytest.mark.asyncio
    async def test_expired_row_is_no_longer_listed_as_pending(self, harness):
        bridge = _make_bridge(harness.store, deadline=60.0)
        sid = await _suspend(bridge, 1)
        harness.set_created_at(sid, time.time() - 600)

        await bridge.expire_overdue(60.0)

        assert [item["request_id"] for item in bridge.pending()] == []
        assert harness.store.list_unresolved() == []
        assert bridge.stats()["pending"] == 0

    @pytest.mark.asyncio
    async def test_second_sweep_is_idempotent(self, harness):
        """同一行不会被拒第二次（库已闭合 ⇒ 不再出现在 list_expired）。"""
        bridge = _make_bridge(harness.store, deadline=60.0)
        sid = await _suspend(bridge, 1)
        harness.set_created_at(sid, time.time() - 600)

        assert len(await bridge.expire_overdue(60.0)) == 1
        assert await bridge.expire_overdue(60.0) == []
        stats = bridge.stats()
        assert stats["rejected"] == 1, "超时拒也是拒：rejected 含它"
        assert stats["auto_reject_timeout"] == 1, "超时是 rejected 的子集"


# ---------------------------------------------------------------------------
# 3.1 并发去重（第 2 轮返工：审查点名的重复裁决/重复计数三条路径）
# ---------------------------------------------------------------------------


class TestConcurrentSweep:
    @pytest.mark.asyncio
    async def test_claimed_row_is_skipped_by_other_sweeper(self, harness):
        """已被别的 worker 认领的到期行，本扫描者一行都不许裁决。

        这是"多 worker（`--workers 2`）下同一行只被一个扫描者裁决"的端到端形态：
        行是真的到期行（`list_expired` 会吐出来），只是认领不在我手上。
        """
        bridge = _make_bridge(harness.store, deadline=60.0)
        sid = await _suspend(bridge, 1)
        harness.set_created_at(sid, time.time() - 600)
        assert harness.store.claim_expired(sid, cutoff_epoch=time.time(), owner="other-worker")

        decisions = await bridge.expire_overdue(60.0)

        assert decisions == []
        stats = bridge.stats()
        assert stats["rejected"] == 0, "别的扫描者认领中，不许把裁决数一遍"
        assert stats[AUTO_REJECT_TIMEOUT_REASON] == 0
        assert harness.snapshot(sid)["status"] == "pending", "库行必须原样留给认领者"

    @pytest.mark.asyncio
    async def test_lease_denied_skips_sweep_without_reading(self):
        """拿不到扫描租约 ⇒ 这轮什么都不做，且**连库都不读**。"""
        stub = _StubStore(lease_denied=True)
        bridge = _make_bridge(stub, deadline=60.0)

        assert await bridge.expire_overdue(60.0) == []

        assert not any(call[0] == "list_expired" for call in stub.calls), (
            "既然另一个 worker 正在扫，读库只是白花一次连接"
        )
        assert bridge.stats()["rejected"] == 0
        assert bridge.stats()[AUTO_REJECT_TIMEOUT_REASON] == 0

    @pytest.mark.asyncio
    async def test_close_failure_does_not_double_count_on_next_sweep(
        self, harness, monkeypatch, warning_logs
    ):
        """审查点名的最小用例：`close_decision` 连续抛错时，第二轮不得再计数。

        重复计数的机制是"闸门已拒 + 库行仍 pending + 下一轮重新扫到"：没有认领
        时第二轮会把同一行再裁决一次（`rejected`/`auto_reject_timeout` 各 +1、
        `record_approval_timeout` 再打一次）。修法是 close 失败**保留认领**，
        TTL 到期后走对账路径按闸门既有裁决补闭合。
        """
        from agentix.policies.hitl import hitl_bridge as bridge_module

        timeout_calls: List[str] = []
        monkeypatch.setattr(bridge_module, "record_approval_timeout", timeout_calls.append)
        bridge = _make_bridge(harness.store, deadline=60.0)
        sid = await _suspend(bridge, 1)
        harness.set_created_at(sid, time.time() - 600)

        real_close = harness.store.close_decision

        def _boom(*args: Any, **kw: Any) -> None:
            raise RuntimeError("simulated close failure")

        monkeypatch.setattr(harness.store, "close_decision", _boom)

        first = await bridge.expire_overdue(60.0)
        assert len(first) == 1
        assert first[0].persist_error, "闸门已拒、库未闭合必须对调用方可见"
        assert bridge.stats()["rejected"] == 1
        assert bridge.stats()[AUTO_REJECT_TIMEOUT_REASON] == 1
        assert timeout_calls == ["tool"], "闸门侧确实落地了一次拒绝 ⇒ 指标照打一次"
        assert harness.snapshot(sid)["status"] == "pending"

        # 第二轮（不 sleep、认领未到期）：必须整轮跳过这一行
        assert await bridge.expire_overdue(60.0) == [], "认领保留 ⇒ 下一轮不得重算"
        assert bridge.stats()["rejected"] == 1, "rejected 不得再 +1"
        assert bridge.stats()[AUTO_REJECT_TIMEOUT_REASON] == 1, "子集计数不得再 +1"
        assert timeout_calls == ["tool"], "超时指标不得再打一次"
        assert harness.snapshot(sid)["status"] == "pending"

        # 认领 TTL 到期 + 库恢复 ⇒ 按闸门既有裁决对账补闭合（不是重新裁决）
        monkeypatch.setattr(harness.store, "close_decision", real_close)
        harness.expire_claim(sid)
        assert await bridge.expire_overdue(60.0) == [], "对账不是新裁决，不进 decisions"

        reconciled = harness.snapshot(sid)
        assert reconciled["status"] == "rejected"
        assert reconciled["decided_by"] == AUTO_REJECT_TIMEOUT_ACTOR, (
            "补闭合用的是闸门里那条既有裁决（谁拒的不能被改写）"
        )
        assert reconciled["decision_reason"] == AUTO_REJECT_TIMEOUT_REASON
        assert (reconciled["claim_owner"], reconciled["claim_until"]) == ("", 0.0)
        assert bridge.stats()["rejected"] == 1
        assert bridge.stats()[AUTO_REJECT_TIMEOUT_REASON] == 1
        assert timeout_calls == ["tool"]
        assert any("reconciled stale pending" in r for r in warning_logs)

    @pytest.mark.asyncio
    async def test_reject_does_not_land_when_gate_already_decided(
        self, harness, warning_logs
    ):
        """闸门已终局 ⇒ 这次拒绝没新落地：不计数、不闭合库行（否则同一次裁决数两遍）。"""
        bridge = _make_bridge(harness.store, deadline=60.0)
        sid = await _suspend(bridge, 1)
        harness.set_created_at(sid, time.time() - 600)
        # 绕过 decide 直接把闸门那条拒掉：模拟"闸门已终局、库行还没跟上"的残留
        assert bridge.gate.reject("gate-1", by="ops") is True

        decision = await bridge.decide(
            sid, ReviewDecisionAction.REJECT, require_gate_pending=True
        )

        assert decision.error == "gate_request_not_pending"
        assert bridge.stats()["rejected"] == 0, "没落地就不许计数"
        assert bridge.stats()[AUTO_REJECT_TIMEOUT_REASON] == 0
        assert harness.snapshot(sid)["status"] == "pending"

        # 下一轮扫描：闸门已决 + 库行 pending ⇒ 对账补偿，不是重新裁决
        assert await bridge.expire_overdue(60.0) == []
        reconciled = harness.snapshot(sid)
        assert reconciled["status"] == "rejected"
        assert reconciled["decided_by"] == "ops", "补偿闭合保留闸门里那位裁决人"
        assert bridge.stats()["rejected"] == 0, "对账不计数"
        assert any("reconciled stale pending" in r for r in warning_logs)


class TestCrossInstanceAndEvictedGate:
    """第 4 轮返工的两条严重项：跨实例裁决 / 闸门记录被 FIFO 逐出。

    两条都只在多 worker（`--workers 2`）形态下必然发生：扫描租约让**只有一个** worker
    扫全部到期行，于是"挂起在 A、被 B 拒掉"是常态；而闸门（内存 FIFO 200）与库
    （真源）是两套生命周期，"库里到期"不代表"闸门里还看得见这条"。
    """

    @pytest.mark.asyncio
    async def test_peer_sweeper_close_purges_memory_and_blocks_redecide(self, harness):
        """严重项 1 最小用例：对端实例拒掉后，本实例待审面收敛且不得再裁一次。

        `A` 挂起并落库；`B`（同库、全新内存与闸门）拿到扫描租约把行拒掉。`A` 的
        `_requests` / `_map` 没人清理——旧实现下 `A.pending()` 仍把这条已超时拒绝的
        pending 当待审列出，运维点 approve 会把库行从 `rejected/system:deadline/
        auto_reject_timeout` 改写成 `approved/ops/manual`（治理事实凭空消失）。
        """
        a = _make_bridge(harness.store, deadline=60.0)
        sid = await _suspend(a, 1)
        harness.set_created_at(sid, time.time() - 600)

        b = HitlBridge(
            gate=HumanReviewGate(enabled=True),
            sidecar=ReviewerSidecar(),
            pending_store=harness.store,
            approval_deadline_s=60.0,
        )
        assert [d.sidecar_request_id for d in await b.expire_overdue(60.0)] == [sid]

        assert sid not in {item["request_id"] for item in a.pending()}, (
            "库行已闭合 ⇒ 本实例的陈旧内存副本不得再出现在待审面"
        )
        assert a.stats()["pending"] == 0

        decision = await a.decide(sid, ReviewDecisionAction.APPROVE, by="ops")
        assert decision.not_found is True, "库行已闭合 ⇒ 内存命中也按 not_found 收口"
        assert decision.approved is False
        assert a.stats()["approved"] == 0, "闸门不许被再批准一次"

        row = harness.snapshot(sid)
        assert row["status"] == "rejected"
        assert row["decided_by"] == AUTO_REJECT_TIMEOUT_ACTOR, "超时归因不许被人工裁决抹掉"
        assert row["decision_reason"] == AUTO_REJECT_TIMEOUT_REASON

    @pytest.mark.asyncio
    async def test_missing_gate_record_is_rebuilt_before_reject(self, harness, monkeypatch):
        """严重项 2 最小用例：闸门记录被 FIFO 逐出后仍必须拒得掉。

        `HumanReviewGate._register` 对超过 `max_requests=200` 的最老记录**不区分是否
        pending** 直接逐出。旧实现把"记录没了"当成"已决"，于是这一行永远拒不掉：
        每轮 `did_not_land` + 释放认领、永久占一个 `max_unresolved` 槽位，还因为按
        `created_at` 升序取数而挡住后面的到期行。修法是按 sidecar 载荷补建后再拒。
        """
        from agentix.policies.hitl import hitl_bridge as bridge_module

        timeout_calls: List[str] = []
        monkeypatch.setattr(bridge_module, "record_approval_timeout", timeout_calls.append)
        bridge = _make_bridge(harness.store, deadline=60.0)
        sid = await _suspend(bridge, 1)
        bridge.gate.clear()  # 模拟 FIFO 逐出：记录"没了"，不是"已决"
        harness.set_created_at(sid, time.time() - 600)

        decisions = await bridge.expire_overdue(60.0)

        assert [d.sidecar_request_id for d in decisions] == [sid], "记录没了也必须拒得掉"
        row = harness.snapshot(sid)
        assert row["status"] == "rejected"
        assert row["decided_by"] == AUTO_REJECT_TIMEOUT_ACTOR
        record = bridge.gate.get("gate-1")
        assert record is not None, "补建后闸门必须留下这条终局记录"
        assert record.decision is ReviewDecision.REJECTED
        stats = bridge.stats()
        assert stats["rejected"] == 1
        assert stats[AUTO_REJECT_TIMEOUT_REASON] == 1, "一次到期拒只计一次"
        assert timeout_calls == ["tool"], "超时指标恰好一次（补建不另开一条裁决路径）"

    @pytest.mark.asyncio
    async def test_decide_fails_closed_when_pending_status_unreadable(
        self, harness, monkeypatch
    ):
        """严重项 1 的另一半：核库行 status 读失败 ⇒ fail-closed，绝不照内存放行。

        与既有跨实例回捞"读不到就不裁"同一条口径：宁可让这次人工裁决失败（运维会
        重试），也不能在不知道库行是否已闭合的情况下把归因改写掉。
        """
        bridge = _make_bridge(harness.store, deadline=60.0)
        sid = await _suspend(bridge, 1)

        def _boom(_sid: str) -> Any:
            raise RuntimeError("simulated db read failure")

        monkeypatch.setattr(harness.store, "load", _boom)

        decision = await bridge.decide(sid, ReviewDecisionAction.REJECT, by="ops")

        assert decision.not_found is True
        assert decision.reason == "pending_status_unreadable"
        assert bridge.stats()["rejected"] == 0, "没核实库行就不许计数"
        assert harness.snapshot(sid)["status"] == "pending", "库行一个字段都不许动"


class TestAttributionValidation:
    @pytest.mark.asyncio
    async def test_attribution_requires_reject(self, harness):
        """归因只对拒绝有意义：给批准贴"超时拒"标签是错的口径，显式拒绝。"""
        bridge = _make_bridge(harness.store, deadline=60.0)
        sid = await _suspend(bridge, 1)

        with pytest.raises(ValueError):
            await bridge.decide(
                sid, ReviewDecisionAction.APPROVE, attribution=AUTO_REJECT_TIMEOUT_REASON
            )
        assert harness.snapshot(sid)["status"] == "pending", "校验失败不得落任何判定"

        ok = await bridge.decide(
            sid, ReviewDecisionAction.REJECT, attribution=AUTO_REJECT_TIMEOUT_REASON
        )
        assert ok.error == "", "拒绝路径仍接受归因（唯一合法用法）"


# ---------------------------------------------------------------------------
# 4. 人工拒 vs 超时拒可区分（验收②，本卡最大做错点）
# ---------------------------------------------------------------------------


class TestManualVersusTimeoutReject:
    @pytest.mark.asyncio
    async def test_same_batch_distinguishes_two_attributions(self, harness):
        bridge = _make_bridge(harness.store, deadline=60.0)
        manual_sid = await _suspend(bridge, 1)
        timeout_sid = await _suspend(bridge, 2)
        harness.set_created_at(manual_sid, time.time() - 600)
        harness.set_created_at(timeout_sid, time.time() - 600)

        manual = await bridge.decide(
            manual_sid, ReviewDecisionAction.REJECT, reason="deny_wrong_table", by="ops"
        )
        expired = await bridge.expire_overdue(60.0)

        assert manual.reason == "deny_wrong_table"
        assert [d.sidecar_request_id for d in expired] == [timeout_sid]

        manual_row = harness.snapshot(manual_sid)
        timeout_row = harness.snapshot(timeout_sid)
        assert manual_row["status"] == timeout_row["status"] == "rejected"
        assert manual_row["decided_by"] == "ops"
        assert timeout_row["decided_by"] == AUTO_REJECT_TIMEOUT_ACTOR
        assert manual_row["decision_reason"] == "deny_wrong_table"
        assert timeout_row["decision_reason"] == AUTO_REJECT_TIMEOUT_REASON

        stats = bridge.stats()
        # rejected 含两种拒（人工 1 + 超时 1）；超时是子集，不进 decided 求和
        assert stats["rejected"] == 2, "rejected 含人工拒与超时拒"
        assert stats["auto_reject_timeout"] == 1, "超时只是其中一条的归因子集"
        assert stats["decided"] == 2, "decided 不许把一次裁决数两遍"
        assert stats["rejection_rate"] == 1.0


# ---------------------------------------------------------------------------
# 4.1 子集计数口径 + 指标面可区分（T19 第 1 轮返工：一般项 5/4）
# ---------------------------------------------------------------------------


class TestSubsetCountingAndMetrics:
    @pytest.mark.asyncio
    async def test_pure_timeout_reject_does_not_double_count_decided(self, harness):
        """口径守卫：一条纯超时拒 = `rejected` 1 / `decided` 1（子集不进求和）。

        `auto_reject_timeout` 若也进 `decided`，这一次裁决就被数两遍，
        `rejection_rate` 的分子分母一起失真——这正是返工要钉住的那条。
        """
        bridge = _make_bridge(harness.store, deadline=60.0)
        sid = await _suspend(bridge, 1)
        harness.set_created_at(sid, time.time() - 600)

        await bridge.expire_overdue(60.0)

        stats = bridge.stats()
        assert stats["rejected"] == 1, "超时拒也是拒"
        assert stats["auto_reject_timeout"] == 1, "子集计数同时 +1"
        assert stats["decided"] == 1, "子集不得再进 decided 求和"
        assert stats["rejection_rate"] == 1.0, "纯超时拒的拒批占比就是 1.0（不被稀释）"

    @pytest.mark.asyncio
    async def test_timeout_metric_is_emitted_for_landed_rejection(self, harness, monkeypatch):
        """一般项 4：超时归因在**指标面**可区分（`retrieval_approval_timeouts_total`）。

        与 `retrieval/usecases/approval_service.py` 同形：裁决指标之外再打一条超时
        指标。`target_type="tool"` 与挂起/桥路径逐字一致（零新增 label）。
        """
        from agentix.policies.hitl import hitl_bridge as bridge_module

        calls: List[str] = []
        monkeypatch.setattr(bridge_module, "record_approval_timeout", calls.append)
        bridge = _make_bridge(harness.store, deadline=60.0)
        sid = await _suspend(bridge, 1)
        harness.set_created_at(sid, time.time() - 600)

        await bridge.expire_overdue(60.0)

        assert calls == ["tool"], "一条到期行 = 一次超时指标，且 target_type 与桥路径逐字一致"

    @pytest.mark.asyncio
    async def test_timeout_metric_is_not_emitted_when_reject_does_not_land(self, monkeypatch):
        """裁决没落下（not_found）就不许打超时指标：不谎报成功。"""
        from agentix.policies.hitl import hitl_bridge as bridge_module

        calls: List[str] = []
        monkeypatch.setattr(bridge_module, "record_approval_timeout", calls.append)
        ghost = {
            "sidecar": {"request_id": "ghost"},
            "sidecar_request_id": "ghost",
            "status": "approved",
        }
        bridge = _make_bridge(_StubStore(rows=[ghost]), deadline=60.0)

        assert await bridge.expire_overdue(60.0) == []
        assert calls == [], "没落地的到期拒绝不计数，也不打超时指标"


# ---------------------------------------------------------------------------
# 4.2 验收③：审批时限与图墙钟互不依赖（严重项 3）
# ---------------------------------------------------------------------------


class TestDeadlineIndependentOfGraphClock:
    @pytest.mark.asyncio
    async def test_deadline_reject_is_independent_of_agent_timeout(self, monkeypatch):
        """两把尺子量纲不同：`AGENT_TIMEOUT` 取值不影响 pending 存活时长的判定。

        `HITL_APPROVAL_DEADLINE_S` 数的是「这条 pending 躺了多久」（库行
        `created_at`），`AGENT_TIMEOUT` 数的是一次图调用的墙钟。挂起本来就不让图
        停住（拦截钩子超时后 executor 已返回 blocked），所以后者取 0.05 秒还是
        10000 秒，都不改变"这条 pending 到期了"这个判定；两者同时可达时**先到者
        生效**、且归因不同（一个留请求级超时错误，一个留可查询的 rejected 审批行）。

        刻意不真起图、不 sleep：本用例量的就是"扫描只看库行的 created_at"。
        """
        for agent_timeout in (0.05, 10000.0):
            # 每次一台新 harness + 新桥：第一台扫描后行已闭合，不能复用
            harness = _new_harness()
            monkeypatch.setattr(settings, "AGENT_TIMEOUT", agent_timeout, raising=False)
            bridge = _make_bridge(harness.store, deadline=60.0)
            sid = await _suspend(bridge, 1)
            harness.set_created_at(sid, time.time() - 600)

            decisions = await bridge.expire_overdue(60.0)

            assert [d.sidecar_request_id for d in decisions] == [sid], (
                f"AGENT_TIMEOUT={agent_timeout} 不该影响到期判定"
            )
            assert decisions[0].reason == AUTO_REJECT_TIMEOUT_REASON
            assert decisions[0].result_action is ReviewDecisionAction.REJECT
            assert harness.snapshot(sid)["decided_by"] == AUTO_REJECT_TIMEOUT_ACTOR

    @pytest.mark.asyncio
    async def test_agent_timeout_shorter_than_deadline_still_sweeps(self, harness, monkeypatch):
        """`AGENT_TIMEOUT < deadline` 的"先到者"形态：扫描照样按期拒，量纲不折算。

        图墙钟比审批时限更短时，最容易做错的两种自伤是：把 `AGENT_TIMEOUT` 当成
        审批时限（于是抢跑早拒），或以为"图早就超时了所以不用拒"（于是永不拒）。
        判定只看库行 `created_at` 与 deadline 的差：躺够 deadline 就拒，没躺够就不拒。
        """
        monkeypatch.setattr(settings, "AGENT_TIMEOUT", 0.05, raising=False)
        bridge = _make_bridge(harness.store, deadline=2.0)
        expired_sid = await _suspend(bridge, 1)
        fresh_sid = await _suspend(bridge, 2)
        harness.set_created_at(expired_sid, time.time() - 5)  # 躺 5 秒 > deadline 2 秒
        harness.set_created_at(fresh_sid, time.time() - 1)  # 躺 1 秒 < deadline 2 秒

        decisions = await bridge.expire_overdue(2.0)

        assert [d.sidecar_request_id for d in decisions] == [expired_sid]
        assert decisions[0].reason == AUTO_REJECT_TIMEOUT_REASON
        assert decisions[0].result_action is ReviewDecisionAction.REJECT
        assert harness.snapshot(expired_sid)["decided_by"] == AUTO_REJECT_TIMEOUT_ACTOR
        assert harness.snapshot(fresh_sid)["status"] == "pending", (
            "AGENT_TIMEOUT 更短不构成提前拒的理由"
        )


# ---------------------------------------------------------------------------
# 5. deadline=0 / store=None（验收①，纪律 1 逐位等价）
# ---------------------------------------------------------------------------


class TestFlagOffEquivalence:
    def test_deadline_zero_keeps_baseline_stats_keys(self):
        assert set(_make_bridge(None).stats()) == BASELINE_STATS_KEYS
        assert set(_make_bridge(None, deadline=0.0).stats()) == BASELINE_STATS_KEYS

    def test_deadline_zero_with_store_adds_only_t18_keys(self, harness):
        stats = _make_bridge(harness.store, deadline=0.0).stats()
        assert set(stats) == STORE_STATS_KEYS
        assert AUTO_REJECT_TIMEOUT_REASON not in stats

    def test_deadline_positive_adds_one_key(self, harness):
        stats = _make_bridge(harness.store, deadline=30.0).stats()
        assert set(stats) == STORE_STATS_KEYS | {AUTO_REJECT_TIMEOUT_REASON}
        assert stats[AUTO_REJECT_TIMEOUT_REASON] == 0

    @pytest.mark.asyncio
    async def test_zero_deadline_returns_empty_and_never_reads_store(self):
        stub = _StubStore(read_fail=True)
        bridge = _make_bridge(stub, deadline=0.0)
        assert await bridge.expire_overdue(0.0) == []
        assert await bridge.expire_overdue(-10) == []
        assert stub.calls == [], "flag 关：一次库都不许读"
        assert set(bridge.stats()) == STORE_STATS_KEYS

    @pytest.mark.asyncio
    async def test_no_store_returns_empty(self):
        """没有库面 ⇒ 不扫、返回空；超时键只反映"这台桥配了时限"，值恒 0。"""
        bridge = _make_bridge(None, deadline=60.0)
        assert await bridge.expire_overdue(60.0) == []
        stats = bridge.stats()
        assert set(stats) == BASELINE_STATS_KEYS | {AUTO_REJECT_TIMEOUT_REASON}
        assert stats[AUTO_REJECT_TIMEOUT_REASON] == 0

    def test_singleton_reads_the_setting(self, monkeypatch):
        """装配点把 `settings.HITL_APPROVAL_DEADLINE_S` 传进桥（单一读取源）。"""
        monkeypatch.setattr(settings, "HITL_APPROVAL_DEADLINE_S", 45.0, raising=False)
        reset_hitl_bridge()
        try:
            assert AUTO_REJECT_TIMEOUT_REASON in get_hitl_bridge().stats()
        finally:
            reset_hitl_bridge()

    def test_singleton_flag_off_has_no_timeout_key(self, monkeypatch):
        monkeypatch.setattr(settings, "HITL_APPROVAL_DEADLINE_S", 0.0, raising=False)
        reset_hitl_bridge()
        try:
            assert AUTO_REJECT_TIMEOUT_REASON not in get_hitl_bridge().stats()
        finally:
            reset_hitl_bridge()

    def test_singleton_and_main_share_one_resolver(self):
        """一般项 6：`HITL_APPROVAL_DEADLINE_S` 只有一个读取函数，两处都调它。

        桥单例与扫描任务必须经同一个函数取值——一处改分辨率、另一处还在自己拼
        `getattr(settings, ...)`，就会出现"装配的 deadline 与扫描的 deadline 不是
        同一个数"这种只有运维才查得出来的漂移（纪律 6 ④）。
        """
        from agentix import main
        from agentix.policies.hitl import hitl_bridge as bridge_module

        assert "resolve_approval_deadline_s" in bridge_module.__all__, (
            "唯一读取函数必须是公开名（内部名前缀会诱使别人再写一个私有副本）"
        )
        assert "resolve_approval_deadline_s" in inspect.getsource(bridge_module.get_hitl_bridge)
        sweeper_src = inspect.getsource(main._start_hitl_deadline_sweeper)
        assert "resolve_approval_deadline_s" in sweeper_src
        assert 'getattr(settings, "HITL_APPROVAL_DEADLINE_S"' not in sweeper_src, (
            "main 侧不许自己读配置键（读取点只许一个）"
        )


# ---------------------------------------------------------------------------
# 6. 失败隔离（单行不阻断 / 读库 fail-open，纪律 2）
# ---------------------------------------------------------------------------


class TestFailureIsolation:
    @pytest.mark.asyncio
    async def test_broken_row_does_not_block_the_others(self, harness, warning_logs):
        # 先在生产口径的 store 上挂一条真 pending，再取它的库行当"好行"
        producer = _make_bridge(harness.store, deadline=60.0)
        good_sid = await _suspend(producer, 1)
        harness.set_created_at(good_sid, time.time() - 600)
        good_row = harness.store.load(good_sid)
        assert good_row is not None
        broken_row = {"sidecar": {}, "sidecar_request_id": "", "status": "pending"}

        # 故障注入：桩库把坏行排在好行前面（坏行必须先被跳过，好行仍要被拒）
        stub = _StubStore(rows=[broken_row, good_row])
        bridge = _make_bridge(stub, deadline=60.0)  # 内存空 ⇒ 走 _apply_loaded_pending 装配

        decisions = await bridge.expire_overdue(60.0)

        assert [d.sidecar_request_id for d in decisions] == [good_sid]
        assert any(
            call[0] == "close_decision" and call[1][0] == good_sid for call in stub.calls
        ), "好行必须真的被闭合"
        assert any("deadline reject failed" in r for r in warning_logs)

    @pytest.mark.asyncio
    async def test_store_read_failure_is_fail_open(self, warning_logs):
        stub = _StubStore(read_fail=True)
        bridge = _make_bridge(stub, deadline=60.0)
        assert await bridge.expire_overdue(60.0) == []
        assert any("could not read expired pendings" in r for r in warning_logs)
        assert bridge.stats()[AUTO_REJECT_TIMEOUT_REASON] == 0

    @pytest.mark.asyncio
    async def test_unlandable_decision_is_not_counted(self, warning_logs):
        """库里有到期行但裁决没落下（decide 返回 not_found）⇒ 不计数、只告警。"""
        # 已决行被桩库当作"到期"吐出：_apply_loaded_pending 不装配已决行，
        # decide 因此落 not_found——这一条绝不能被当成"已超时拒绝"计数。
        ghost = {
            "sidecar": {"request_id": "ghost"},
            "sidecar_request_id": "ghost",
            "status": "approved",
        }
        stub = _StubStore(rows=[ghost])
        bridge = _make_bridge(stub, deadline=60.0)

        assert await bridge.expire_overdue(60.0) == []
        assert bridge.stats()[AUTO_REJECT_TIMEOUT_REASON] == 0
        assert bridge.stats()["rejected"] == 0
        assert any("did not land" in r for r in warning_logs), "未落地必须显式可见"


# ---------------------------------------------------------------------------
# 7. main.py 扫描任务（纯函数 / 起停 / 循环）
# ---------------------------------------------------------------------------


class TestSweepInterval:
    def test_small_deadline_hits_floor(self):
        from agentix import main

        assert main._hitl_sweep_interval(2.0) == 1.0  # 2/4 = 0.5 → 夹到 1.0

    def test_mid_deadline_is_a_quarter(self):
        from agentix import main

        assert main._hitl_sweep_interval(60.0) == 15.0

    def test_large_deadline_hits_ceiling(self):
        from agentix import main

        assert main._hitl_sweep_interval(3600.0) == 30.0


class _StubBridge:
    """扫描循环用的桥桩：只暴露 `expire_overdue` 并记录每轮入参。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: List[float] = []
        self._fail = fail

    async def expire_overdue(self, deadline_s: float, **_: Any) -> List[Any]:
        self.calls.append(deadline_s)
        if self._fail:
            raise RuntimeError("simulated sweep failure")
        return []


class TestSweeperWiring:
    def _reset_handle(self, monkeypatch):
        from agentix import main

        monkeypatch.setattr(main, "_hitl_deadline_task", None)
        monkeypatch.setattr(main, "_hitl_deadline_bridge", None)

    @pytest.mark.asyncio
    async def test_flag_off_creates_no_task(self, monkeypatch):
        from agentix import main

        self._reset_handle(monkeypatch)
        monkeypatch.setattr(settings, "HITL_APPROVAL_DEADLINE_S", 0.0, raising=False)
        monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", True, raising=False)
        monkeypatch.setattr(settings, "HITL_PENDING_PERSIST_ENABLED", True, raising=False)

        assert main._start_hitl_deadline_sweeper(_StubBridge()) is False
        assert main._hitl_deadline_task is None

    @pytest.mark.asyncio
    async def test_deadline_without_persistence_warns_and_creates_no_task(
        self, monkeypatch, warning_logs
    ):
        from agentix import main

        self._reset_handle(monkeypatch)
        monkeypatch.setattr(settings, "HITL_APPROVAL_DEADLINE_S", 60.0, raising=False)
        monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", True, raising=False)
        monkeypatch.setattr(settings, "HITL_PENDING_PERSIST_ENABLED", False, raising=False)

        assert main._start_hitl_deadline_sweeper(_StubBridge()) is False
        assert main._hitl_deadline_task is None
        assert any("HITL_APPROVAL_DEADLINE_S=60.0" in r for r in warning_logs), (
            "配了却不生效必须显式告警一次（不静默假装在管）"
        )

    @pytest.mark.asyncio
    async def test_both_flags_on_creates_cancellable_task(self, monkeypatch):
        from agentix import main

        self._reset_handle(monkeypatch)
        monkeypatch.setattr(settings, "HITL_APPROVAL_DEADLINE_S", 60.0, raising=False)
        monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", True, raising=False)
        monkeypatch.setattr(settings, "HITL_PENDING_PERSIST_ENABLED", True, raising=False)

        assert main._start_hitl_deadline_sweeper(_StubBridge()) is True
        task = main._hitl_deadline_task
        assert task is not None and not task.done()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
        monkeypatch.setattr(main, "_hitl_deadline_task", None)

    @pytest.mark.asyncio
    async def test_human_review_off_warns_and_creates_no_task(self, monkeypatch, warning_logs):
        """严重项 2：HUMAN_REVIEW_ENABLED=false 时没有桥/没有 pending，不建任务且告警。"""
        from agentix import main

        self._reset_handle(monkeypatch)
        monkeypatch.setattr(settings, "HITL_APPROVAL_DEADLINE_S", 60.0, raising=False)
        monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", False, raising=False)
        monkeypatch.setattr(settings, "HITL_PENDING_PERSIST_ENABLED", True, raising=False)

        assert main._start_hitl_deadline_sweeper(_StubBridge()) is False
        assert main._hitl_deadline_task is None
        assert any("HUMAN_REVIEW_ENABLED" in r for r in warning_logs), (
            "前置条件不满足必须点名是哪个开关（否则运维只会怀疑 deadline 本身）"
        )

    @pytest.mark.asyncio
    async def test_missing_bridge_warns_and_creates_no_task(self, monkeypatch, warning_logs):
        """严重项 2 防御支：三开关齐备但 hook 安装失败（bridge=None）⇒ 不扫 None。"""
        from agentix import main

        self._reset_handle(monkeypatch)
        monkeypatch.setattr(settings, "HITL_APPROVAL_DEADLINE_S", 60.0, raising=False)
        monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", True, raising=False)
        monkeypatch.setattr(settings, "HITL_PENDING_PERSIST_ENABLED", True, raising=False)

        assert main._start_hitl_deadline_sweeper(None) is False
        assert main._hitl_deadline_task is None
        assert any("unavailable" in r for r in warning_logs), "桥缺失必须显式可见"

    def test_startup_wires_the_sweeper_to_the_installed_bridge(self):
        """接线守卫：`_startup` 接住 install_executor_hook 的返回值并起扫描任务；接线经 `asyncio.to_thread` 下放到线程，同步读库不进事件循环。"""
        from agentix import main

        source = inspect.getsource(main._startup)
        assert "bridge = await asyncio.to_thread(install_executor_hook)" in source
        assert "_start_hitl_deadline_sweeper(bridge)" in source


class TestSweeperLoop:
    @pytest.mark.asyncio
    async def test_loop_keeps_going_after_a_failure(self, warning_logs):
        from agentix import main

        stub = _StubBridge(fail=True)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(main._hitl_deadline_sweeper(stub, 60.0, 0.01), timeout=0.2)

        assert len(stub.calls) >= 2, "一次失败不许让扫描任务静默死掉"
        assert any("deadline sweep failed" in r for r in warning_logs)

    @pytest.mark.asyncio
    async def test_loop_passes_deadline_through(self):
        from agentix import main

        stub = _StubBridge()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(main._hitl_deadline_sweeper(stub, 45.0, 0.01), timeout=0.1)
        assert stub.calls and set(stub.calls) == {45.0}


class TestShutdownCancelsSweeper:
    @pytest.mark.asyncio
    async def test_shutdown_cancels_task_and_clears_handle(self, monkeypatch):
        from agentix import main

        task = asyncio.create_task(asyncio.sleep(3600))
        monkeypatch.setattr(main, "_hitl_deadline_task", task)
        # 关停的其余动作与本卡无关，且会触到真资源：只留一条最小桩（lightrag 单例）。
        class _NoopService:
            async def cleanup(self) -> None:
                return None

        monkeypatch.setattr(main, "get_lightrag_service", lambda: _NoopService())

        await main._shutdown()

        assert task.cancelled()
        assert main._hitl_deadline_task is None

    @pytest.mark.asyncio
    async def test_shutdown_without_task_is_noop(self, monkeypatch):
        from agentix import main

        monkeypatch.setattr(main, "_hitl_deadline_task", None)
        monkeypatch.setattr(main, "_hitl_deadline_bridge", None)

        class _NoopService:
            async def cleanup(self) -> None:
                return None

        monkeypatch.setattr(main, "get_lightrag_service", lambda: _NoopService())
        await main._shutdown()  # 不抛即通过（flag 关时关停路径与基线一致）
        assert main._hitl_deadline_task is None

    @pytest.mark.asyncio
    async def test_shutdown_releases_sweep_lease(self, monkeypatch):
        """第 2 轮返工：关停必须让出扫描租约，否则另一个 worker 空等一整个 TTL。

        句柄里存的是**起任务时那台桥**：关停路径不许调 `get_hitl_bridge()`
        重新装配单例（那会凭空造一个带库面/指标面的对象）。
        """
        from agentix import main

        task = asyncio.create_task(asyncio.sleep(3600))

        class _FakeBridge:
            def __init__(self) -> None:
                self.releases = 0

            def release_sweep_lease(self) -> bool:
                self.releases += 1
                return True

        fake = _FakeBridge()
        monkeypatch.setattr(main, "_hitl_deadline_task", task)
        monkeypatch.setattr(main, "_hitl_deadline_bridge", fake)

        class _NoopService:
            async def cleanup(self) -> None:
                return None

        monkeypatch.setattr(main, "get_lightrag_service", lambda: _NoopService())

        await main._shutdown()

        assert fake.releases == 1, "关停必须让出扫描租约一次"
        assert main._hitl_deadline_bridge is None
        assert main._hitl_deadline_task is None
