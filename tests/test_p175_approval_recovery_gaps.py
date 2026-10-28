"""P175（T28）：审批/恢复四类覆盖缺口的补齐用例。

对应规划 `docs/design/审批与中断恢复优化规划.md` §4.3 T28 卡。四类缺口是
T18–T23 能力落地之后才可测的**行为面**，既有用例各覆盖了其中一半：

| 类 | 既有覆盖（不重复） | 本文件补的那一面 |
| --- | --- | --- |
| ① 人审无响应超时 | `test_p165_hitl_approval_deadline.py` 用**手工调用** `expire_overdue`（harness）与**桩桥**（`_StubBridge`）验证到期拒、归因、租约、认领 | **真扫描循环 + 真桥 + 真库**：无人裁决的一条 pending 由后台 `main._hitl_deadline_sweeper` 自己（没有任何一处手动调用）拒掉，且闭合后继续空转不重复计数 |
| ② 真实重启丢 pending | `test_p136_hitl_pending_persistence.py::TestStartupRestore` 在**同一进程**内重建 bridge（sqlite 内存库，`StaticPool`） | **两个真实操作系统进程**共用一个 sqlite **文件**库：A 落库后进程退出，B 新进程 `restore_pending()` 后裁决 |
| ③ 多副本路由 | `test_p136_...::TestCrossInstanceDecide` 同一进程、同一个 store 对象、两个 bridge 实例 | 同 ②，但实例 2 **不跑 restore**：内存面为空（`stats()["pending"] == 0`）时按库行裁决成功 |
| ④ 流式审批 | `test_p174_stream_hitl.py` 在 `ChatService.stream_events` 的 **service 层 dict 帧**上验证 approval/heartbeat 帧与 idle 语义；`test_p98` 只钉了 `meta`/`done`/`error` 这些关键帧 | **SSE 出口链**：service 帧 → `stream_gate.gate_events`（有界队列背压开）→ `interfaces.http.v1.chat._to_sse` → wire 上的 `data: {...}` 行；审批帧在背压下**一帧都不许丢**，心跳帧本来就是可丢的占位帧 |

验收口径（T28 卡）：① 四类各至少一条用例且**红绿可判定**（见下「红绿判据」）；
② 重启/多副本类用**真实进程**（卡内明令「库级自转不算」，本文件不做同进程重建
store 的等价物）；③ 动测试基线前已获维护者同意（`.qoder/rules/task-order.md`
§8.3 把 T28 列为步 2 HITL 泳道任务，授权范围是**只新增测试文件**，本文件不改任何
既有测试的断言或基线数字）。

红绿判据（每一类都靠"把被测行为关掉就变红"证明不是空转）：

1. ① 把扫描循环的 deadline 传成 `0.0`（等价于特性关）⇒ `expire_overdue` 立即返回
   `[]` ⇒ 库行永不闭合 ⇒ `test_real_sweeper_loop_auto_rejects_untouched_pending` 红；
   恢复后绿。
2. ② 让进程 A 写到**另一个**库文件 ⇒ 进程 B `restore_pending() == 0` ⇒ 断言红；
   恢复后绿。
3. ③ 同 ②：实例 2 面对空库 ⇒ `listing_has_pending` False、`decide` 走 `not_found`
   ⇒ 断言红；恢复后绿。
4. ④ 把 `STREAM_HITL_ENABLED` 关掉 ⇒ 上游 `build_stream_hitl` 返回 None ⇒ 流里既没有
   `stream_hitl` 通道也没有 approval 帧 ⇒ 断言红；恢复后绿。

**skip 理由（按设计，不是漏测）**：②③ 标 `integration`（真起操作系统进程 + 真写
sqlite 文件库），CI 的 `-m "not integration"` 会跳过它们，本地必须能跑。本文件**不**
依赖任何 POSIX-only 能力（`PR_SET_PDEATHSIG` 之类，见 `.qoder/rules/code-exec.md` §6），
因此**没有**平台 skip 分支——四类在 Windows 上全部实际执行。

运行：`.venv/Scripts/python.exe -m pytest tests/test_p175_approval_recovery_gaps.py -q`
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from agentix.application.services.chat_service import ChatService
from agentix.application.services.stream_hitl import (
    APPROVAL_FRAME_TYPE,
    HEARTBEAT_FRAME_TYPE,
    StreamHitlChannel,
)
from agentix.application.services.trajectory import reset_trajectory_recorder
from agentix.config import settings
from agentix.infrastructure.persistence.hitl_pending_store import HitlPendingStore
from agentix.policies.hitl.hitl_bridge import (
    AUTO_REJECT_TIMEOUT_ACTOR,
    AUTO_REJECT_TIMEOUT_REASON,
    HitlBridge,
    reset_hitl_bridge,
)
from agentix.policies.hitl.sidecar import ReviewDecisionAction, ReviewerSidecar
from agentix.runtime.control_plane import reset_control_plane
from agentix.tools.gateway.human_review import (
    HumanReviewGate,
    ReviewRequest,
    RiskLevel,
    get_review_gate,
    mark_decision_surface,
    reset_review_gate,
)

REPO = Path(__file__).resolve().parents[1]

#: 类①的时限与扫描间隔：都取毫秒级，用例在 1 秒内可判定
DEADLINE_S = 0.05
INTERVAL_S = 0.02

#: 类④的心跳间隔 / 消费者滞后 / 裁决延迟（毫秒级，便于在秒级内断言）。
#: 滞后与裁决延迟都刻意跨过两个 `stream_hitl.DECISION_POLL_S`（0.25s）轮询点：
#: 第一拍只发审批帧、第二拍先查裁决——只有跨过两拍才会真的落到"发心跳"那一拍。
HEARTBEAT_S = 0.05
CONSUMER_LAG_S = 0.6
DECIDE_DELAY_S = 1.4


# ---------------------------------------------------------------------------
# 夹具：单例隔离（桥 / 闸门 / 控制面 / 轨迹），teardown 必还原
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_singletons():
    reset_control_plane()
    reset_trajectory_recorder()
    reset_review_gate()
    reset_hitl_bridge()
    yield
    reset_review_gate()
    reset_hitl_bridge()
    reset_control_plane()
    reset_trajectory_recorder()


@pytest.fixture
def hitl_env(monkeypatch: pytest.MonkeyPatch):
    """三层 flag 全开（生产装配的前置组合）+ 决策面已接线。"""
    monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", True)
    monkeypatch.setattr(settings, "HITL_IN_LOOP_ENABLED", True)
    monkeypatch.setattr(settings, "STREAM_HITL_ENABLED", True)
    monkeypatch.setattr(settings, "STREAM_HITL_HEARTBEAT_S", HEARTBEAT_S)
    mark_decision_surface()
    yield


def _request(request_id: str = "p175-gate-1") -> ReviewRequest:
    """直造一条被拦下的审批请求（与执行点 ``preflight`` 的产物同形）。"""
    return ReviewRequest(
        request_id=request_id,
        tool_name="delete_records",
        risk_level=RiskLevel.HIGH,
        reason="review_required",
        args_digest={"table": "orders"},
    )


async def _wait_until(predicate, *, timeout_s: float = 3.0, tick_s: float = 0.01) -> bool:
    """轮询直到 predicate 为真（只用内存面，避免与扫描线程并发读库）。"""
    until = time.monotonic() + timeout_s
    while time.monotonic() < until:
        if predicate():
            return True
        await asyncio.sleep(tick_s)
    return False


async def _cancel(task: "asyncio.Task[Any]") -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _file_store(db_path: Path) -> HitlPendingStore:
    """文件库 store（跨进程共用同一个 sqlite 文件；`timeout` 兜 Windows 文件锁）。"""
    engine = create_engine(
        "sqlite:///" + db_path.as_posix(),
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    return HitlPendingStore(session_factory=sessionmaker(bind=engine, expire_on_commit=False))


# ---------------------------------------------------------------------------
# 类① 人审无响应超时：真扫描循环自己把无人裁决的 pending 拒掉
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_sweeper_loop_auto_rejects_untouched_pending(tmp_path):
    """没有任何一处手动调用 `expire_overdue`：后台循环按真实墙钟到期拒。

    红绿判据：把 `_hitl_deadline_sweeper` 的 deadline 传成 0 ⇒ 行永不闭合 ⇒ 红。
    """
    from agentix import main

    store = _file_store(tmp_path / "sweep.db")
    store.ensure_ready()
    gate = HumanReviewGate(enabled=True)
    bridge = HitlBridge(
        gate=gate,
        sidecar=ReviewerSidecar(),
        pending_store=store,
        approval_deadline_s=DEADLINE_S,
    )
    # 生产等价：闸门记录由执行点 preflight 建好后，钩子才把请求转成 pending
    request = _request("p175-sweep")
    gate.restore_request(request)
    suspend = await bridge.open_from_gate(request, {"table": "orders"}, thread_id="t28-sweep")

    assert suspend.persist_error == "", "挂起必须真的落库（否则本用例测的就不是库面）"
    assert bridge.stats()["pending"] == 1
    assert store.load(suspend.sidecar_request_id)["status"] == "pending"

    # 真循环：与 `main._start_hitl_deadline_sweeper` 起的同一个协程
    task = asyncio.create_task(main._hitl_deadline_sweeper(bridge, DEADLINE_S, INTERVAL_S))
    try:
        closed = await _wait_until(lambda: bridge.stats()[AUTO_REJECT_TIMEOUT_REASON] >= 1)
        assert closed, (
            f"{DEADLINE_S}s 时限内无人裁决，扫描循环必须在 3s 内到期拒；"
            f"实得 stats={bridge.stats()}"
        )

        # 闭合是**循环**干的，不是循环退出后补的
        assert not task.done(), "扫描任务必须还活着（到期拒发生在循环内部）"

        # 再空转若干轮：已决行不得被二次裁决（同一轮扫描不重复计数）
        first_round = bridge.stats()[AUTO_REJECT_TIMEOUT_REASON]
        await asyncio.sleep(INTERVAL_S * 6)
        stats = bridge.stats()
        assert first_round == 1
        assert stats[AUTO_REJECT_TIMEOUT_REASON] == 1, "同一次到期拒只许计一次"
        assert stats["rejected"] == 1, "rejected 是裁决桶，超时拒不得重复进桶"
        assert stats["decided"] == 1
        assert stats["rejection_rate"] == 1.0
        assert stats["pending"] == 0, "闭合后内存待审面必须归零"
    finally:
        await _cancel(task)

    # 三处归因同源：库行 decided_by / decision_reason + stats 子集计数
    row = store.load(suspend.sidecar_request_id)
    assert row["status"] == "rejected"
    assert row["decided_by"] == AUTO_REJECT_TIMEOUT_ACTOR == "system:deadline"
    assert row["decision_reason"] == AUTO_REJECT_TIMEOUT_REASON == "auto_reject_timeout"
    assert row["decided_at"] is not None
    # 待审面（GET /ops/hitl/approvals 的 pending 来源）不再挂着它
    assert bridge.pending() == []
    # 治理层终局：闸门侧真的落了拒绝（不是只在库里写了行）
    record = gate.get("p175-sweep")
    assert record is not None and record.decided_by == AUTO_REJECT_TIMEOUT_ACTOR


# ---------------------------------------------------------------------------
# 类② / 类③：真实操作系统进程（同一 sqlite 文件库）
# ---------------------------------------------------------------------------

#: 子进程实例脚本：一个进程只做一件事，结果写文件（避免与日志混在 stdout）
_CHILD_SCRIPT = '''
"""T28 真进程实例：一条 pending 的生命周期（由 tests/test_p175 起真进程跑）。

argv: <mode> <db_path> <out_path> [sidecar_request_id]
  create  —— 进程 A：把一条 pending 落库后退出（不裁决、不等待）
  restart —— 进程 B（真重启）：restore_pending() 之后按库行裁决
  cold    —— 进程 B'（等价双实例）：不 restore，直接按库行裁决
"""
import asyncio
import json
import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from agentix.infrastructure.persistence.hitl_pending_store import HitlPendingStore
from agentix.policies.hitl.hitl_bridge import HitlBridge
from agentix.policies.hitl.sidecar import ReviewDecisionAction, ReviewerSidecar
from agentix.tools.gateway.human_review import HumanReviewGate, ReviewRequest, RiskLevel

GATE_ID = "gate-t28-process"


def make_store(path):
    engine = create_engine(
        "sqlite:///" + path,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    return HitlPendingStore(session_factory=sessionmaker(bind=engine, expire_on_commit=False))


def main():
    mode, db_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    store = make_store(db_path)
    gate = HumanReviewGate(enabled=True)
    bridge = HitlBridge(gate=gate, sidecar=ReviewerSidecar(), pending_store=store)
    result = {"mode": mode, "pid": os.getpid()}
    if mode == "create":
        request = ReviewRequest(
            request_id=GATE_ID,
            tool_name="delete_records",
            risk_level=RiskLevel.HIGH,
            reason="review_required",
            args_digest={"table": "orders"},
        )
        gate.restore_request(request)
        suspend = asyncio.run(
            bridge.open_from_gate(request, {"table": "orders"}, thread_id="t28")
        )
        result["sidecar_request_id"] = suspend.sidecar_request_id
        result["persist_error"] = suspend.persist_error
        result["gate_request_id"] = suspend.gate_request_id
    else:
        sid = sys.argv[4]
        result["sidecar_request_id"] = sid
        result["stats_pending_before"] = bridge.stats()["pending"]
        result["restored"] = bridge.restore_pending() if mode == "restart" else -1
        result["stats_pending_after_restore"] = bridge.stats()["pending"]
        result["listing_has_pending"] = sid in {it["request_id"] for it in bridge.pending()}
        before = store.load(sid)
        result["row_status_before"] = None if before is None else before["status"]
        decision = asyncio.run(bridge.decide(sid, ReviewDecisionAction.APPROVE, by="ops"))
        result["not_found"] = decision.not_found
        result["approved"] = decision.approved
        after = store.load(sid)
        result["row_status_after"] = None if after is None else after["status"]
        result["decided_by"] = None if after is None else after["decided_by"]
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False)
    return 0


sys.exit(main())
'''


def _run_child(tmp_path: Path, *args: str) -> None:
    """起一个**真实子进程**跑 `_CHILD_SCRIPT`；非 0 退出即失败（不掩盖 stderr）。"""
    script = tmp_path / "hitl_instance.py"
    script.write_text(_CHILD_SCRIPT, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, str(script), *args],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    assert proc.returncode == 0, (
        f"子进程 {args[0]} 失败 rc={proc.returncode}\n--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}"
    )


def _read_result(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.integration
class TestRealProcessRestart:
    """类②：**真实进程重启**丢不丢 pending（同进程重建 store 不算，本类不提供该退化）。"""

    def test_pending_survives_real_process_restart_and_is_decideable(self, tmp_path):
        db = tmp_path / "restart.db"
        out_a = tmp_path / "a.json"
        out_b = tmp_path / "b.json"

        # 进程 A：挂起一条 pending 后退出
        _run_child(tmp_path, "create", str(db), str(out_a))
        a = _read_result(out_a)
        sid = a["sidecar_request_id"]
        assert sid, "进程 A 必须真的挂起并给出 sidecar id"
        assert a["persist_error"] == "", f"落库失败则本用例无意义：{a['persist_error']}"

        # 进程 A 已退出：内存面随进程消失，只剩库文件里的这一行
        row_before = _file_store(db).load(sid)
        assert row_before is not None and row_before["status"] == "pending"
        assert row_before["gate_request_id"] == a["gate_request_id"]

        # 进程 B：全新进程（新解释器、新内存、同一库文件）restore 后裁决
        _run_child(tmp_path, "restart", str(db), str(out_b), sid)
        b = _read_result(out_b)
        assert b["pid"] != a["pid"], "必须是两个不同的操作系统进程（同进程重开 store 不算重启）"
        assert b["restored"] == 1, "启动期 restore 必须把那条未决 pending 拉回内存"
        assert b["stats_pending_before"] == 0, "进程 B 起始内存面必须为空（真冷启动）"
        assert b["listing_has_pending"] is True
        assert b["row_status_before"] == "pending"
        assert b["not_found"] is False, "重启后裁决不得退化成 404"
        assert b["approved"] is True
        assert b["row_status_after"] == "approved"
        assert b["decided_by"] == "ops"

        # 第三方复核（进程 B 也已退出）：闭合真的落进了文件库
        assert _file_store(db).load(sid)["status"] == "approved"


@pytest.mark.integration
class TestMultiReplicaRouting:
    """类③：两个实例共享库，实例 2 内存里没有该 pending 也能裁决（不是 404）。"""

    def test_second_instance_decides_from_store_without_restore(self, tmp_path):
        db = tmp_path / "replica.db"
        out_a = tmp_path / "a.json"
        out_b = tmp_path / "b.json"

        _run_child(tmp_path, "create", str(db), str(out_a))
        a = _read_result(out_a)
        sid = a["sidecar_request_id"]
        assert sid and a["persist_error"] == ""

        # 实例 2：不跑 restore（与类②区分——它测的就是"内存没有也认库"）
        _run_child(tmp_path, "cold", str(db), str(out_b), sid)
        b = _read_result(out_b)
        assert b["pid"] != a["pid"]
        # 内存面判据用 stats()["pending"]：`pending()` 会**并库面**（T18 验收③要求
        # 跨实例的 GET 也能点名），所以它必然含该 id，不能用它判"内存里有没有"
        assert b["stats_pending_before"] == 0, "实例 2 的内存待审面必须是空的"
        assert b["restored"] == -1, "本用例刻意不 restore（走的就是库级回捞通道）"
        assert b["stats_pending_after_restore"] == 0
        assert b["listing_has_pending"] is True, "跨实例待审面必须列出它（运维才拿得到 id）"
        assert b["row_status_before"] == "pending"
        assert b["not_found"] is False, "跨实例裁决必须是判定成功，不是 404"
        assert b["approved"] is True
        assert b["row_status_after"] == "approved"
        assert b["decided_by"] == "ops"

        # 库行闭合（实例 1 已退出，由本进程旁证）
        assert _file_store(db).load(sid)["status"] == "approved"


# ---------------------------------------------------------------------------
# 类④ 流式审批：service 帧 → SSE 出口门控 → wire 上的 data: 行
# ---------------------------------------------------------------------------


class _StubStreamGraph:
    """按预设回放事件；在中间用 config 里的流内通道挂起等裁决。

    通道由 `build_stream_hitl` 在 `ChatService.stream_events` 内注入——flag-off 时
    压根没有该键，这段"等裁决"不存在（与生产 flag-off 逐位等价）。
    """

    def __init__(
        self,
        *,
        pre: Optional[List[Dict[str, Any]]] = None,
        approval: Optional[Dict[str, Any]] = None,
        post: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self._pre = pre or []
        self._approval = approval
        self._post = post or []
        self.channel: Optional[StreamHitlChannel] = None
        self.decision: Optional[str] = None
        self.seen_config: Optional[Dict[str, Any]] = None

    def astream_events(self, input_state, *, config=None, version=None, **kwargs):
        pre, approval, post = self._pre, self._approval, self._post
        self.seen_config = config

        async def _gen():
            for event in pre:
                yield event
            if approval is not None:
                channel = (config or {}).get("configurable", {}).get("stream_hitl")
                if channel is not None:
                    self.channel = channel
                    self.decision = await channel.request(approval)
            for event in post:
                yield event

        return _gen()


def _done_event() -> Dict[str, Any]:
    return {
        "event": "on_chain_end",
        "name": "LangGraph",
        "data": {"output": {"sources": [{"document_id": "d1"}]}},
        "metadata": {},
    }


def _token_event(text: str) -> Dict[str, Any]:
    class _Chunk:
        def __init__(self, content: Any) -> None:
            self.content = content

    return {
        "event": "on_chat_model_stream",
        "name": "ChatModel",
        "data": {"chunk": _Chunk(text)},
        "metadata": {"langgraph_node": "create_kb_query"},
    }


async def _collect_wire(
    graph: _StubStreamGraph,
    *,
    lag_after_pending_s: float = 0.0,
    session_id: str = "s-p175",
) -> List[Dict[str, Any]]:
    """走真实出口链：`ChatService.stream_events` → `gate_events` → `_to_sse` → wire 行。

    `lag_after_pending_s` 模拟慢消费者（浏览器/网络慢）：收到**审批 pending 帧之后**
    停这么久不取下一帧，把有界队列灌满，从而真正踩到背压的丢弃分支。

    滞后点刻意选在 pending 帧之后而不是第一帧：审批帧是关键帧，生产者会
    `await queue.put` 阻塞——若在第一帧就滞后，整个源都被按住，心跳帧压根不会被
    发出来，也就测不到"心跳可丢、审批不可丢"这条分工。
    """
    from agentix.application.services.stream_gate import gate_events
    from agentix.interfaces.http.v1.chat import _to_sse

    service = ChatService(graph_getter=lambda: graph)
    wire: List[Dict[str, Any]] = []
    lagged = False
    async for event in gate_events(service.stream_events("T28 审批帧", session_id)):
        chunk = _to_sse(event)
        assert chunk.startswith("data: ") and chunk.endswith(
            "\n\n"
        ), f"SSE 帧格式必须逐字为 'data: {{json}}\\n\\n'，实得 {chunk!r}"
        frame = json.loads(chunk[len("data: ") :].strip())
        wire.append(frame)
        is_pending_approval = frame.get("type") == APPROVAL_FRAME_TYPE and (
            (frame.get("metadata") or {}).get("status") == "pending"
        )
        if lag_after_pending_s and is_pending_approval and not lagged:
            lagged = True
            await asyncio.sleep(lag_after_pending_s)
    return wire


def _frames(wire: List[Dict[str, Any]], frame_type: str) -> List[Dict[str, Any]]:
    return [frame for frame in wire if frame.get("type") == frame_type]


def _sample(name: str, **labels: Any) -> float:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.mark.asyncio
async def test_approval_frames_reach_the_sse_wire_under_backpressure(hitl_env, monkeypatch):
    """待审期在 wire 上能看到 approval(pending)→approval(decided)，背压一帧都不丢。

    红绿判据：关掉 `STREAM_HITL_ENABLED` ⇒ 没有流内通道 ⇒ 没有 approval 帧 ⇒ 红。
    """
    monkeypatch.setattr(settings, "SSE_QUEUE_SIZE", 1)  # 有界队列 = 1：一帧就满
    monkeypatch.setattr(settings, "SSE_COALESCE_ENABLED", False)
    gate = get_review_gate()
    pending = _request("p175-stream")
    gate.restore_request(pending)

    graph = _StubStreamGraph(
        pre=[_token_event("审批前")],
        approval={
            "request_id": pending.request_id,
            "tool": pending.tool_name,
            "risk_level": "HIGH",
        },
        post=[_token_event("审批后"), _done_event()],
    )
    dropped_before = _sample("sse_dropped_frames_total", reason="backpressure")

    async def _approve_later() -> None:
        await asyncio.sleep(DECIDE_DELAY_S)
        gate.approve(pending.request_id, by="ops")

    decider = asyncio.ensure_future(_approve_later())
    try:
        wire = await _collect_wire(
            graph,
            lag_after_pending_s=CONSUMER_LAG_S,
            session_id="s-p175-stream",
        )
    finally:
        await decider

    assert graph.channel is not None, "flag 全开时节点必须拿到流内审批通道"
    channel = graph.channel
    assert graph.decision == "approve"

    approvals = _frames(wire, APPROVAL_FRAME_TYPE)
    assert [frame["metadata"]["status"] for frame in approvals] == ["pending", "decided"]
    assert approvals[0]["metadata"]["request_id"] == pending.request_id
    assert approvals[0]["metadata"]["tool"] == pending.tool_name
    assert approvals[1]["metadata"]["decision"] == "approve"
    # 审批帧是**关键帧**：慢消费者（队满）下也不许丢——丢了这场运行就没人可批
    assert (
        len(approvals) == channel.approvals_emitted == 2
    ), "wire 上的审批帧数必须等于通道发出数（背压不得丢审批帧）"

    # 心跳帧是**可丢**的占位帧：队满时被丢属于设计，且必须计数可见
    heartbeats = _frames(wire, HEARTBEAT_FRAME_TYPE)
    assert channel.heartbeats_emitted >= 2, "等待期必须真的发过心跳（跨过至少两拍）"
    for beat in heartbeats:
        assert beat["metadata"]["status"] == "waiting_approval"
        assert "waited_s" in beat["metadata"]
    assert len(heartbeats) <= channel.heartbeats_emitted, "不许出现通道没发却上了线的帧"
    dropped_delta = _sample("sse_dropped_frames_total", reason="backpressure") - dropped_before
    assert dropped_delta >= 1, "队满必须真的发生（否则这条用例没踩到背压分支）"
    assert len(heartbeats) < channel.heartbeats_emitted, "心跳是可丢帧：背压下必须有帧被丢"

    # 顺序契约：审批帧在图继续之前，done 恰好一次且在最末
    types = [str(frame.get("type")) for frame in wire]
    assert types.index("approval") < types.index("done")
    assert types.count("done") == 1 and types[-1] == "done"
    # 裁决后图继续：审批前后的 token 都上了 wire
    content = "".join(
        str(frame.get("content") or "") for frame in wire if frame.get("type") == "message"
    )
    assert content == "审批前审批后"
