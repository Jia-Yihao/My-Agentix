"""T23 流内审批（``STREAM_HITL_ENABLED``，默认关）：审批帧 / 心跳 / 裁决后继续。

覆盖面与验收口径（对应规划 approval-recovery §4 T23 的四条验收标准）：

1. **审批帧可下发、可裁决、裁决后流继续**：节点内 ``review_interrupt`` 等待期间
   流侧下发 ``approval`` 帧；裁决（``bridge.decide`` 等价路径：闸门簿记）落地后
   节点拿到 ``approve`` 并继续，流以 ``done`` 收口（``TestApprovalFrames``）。
2. **关 flag 时 SSE 帧序列逐位一致**：同一条 stub 图在 flag-off 下的帧序列与
   硬编码基线逐帧相同，且 config 不多出任何 T23 键、帧里没有 approval/heartbeat
   （``TestFlagOffEquivalence``）。
3. **审批等待不冻结心跳**：等待时长超过 ``STREAM_IDLE_TIMEOUT`` 也不判 idle
   （无 error 帧、心跳帧数量达标）；无审批时的静默仍按同一阈值判 idle
   （``TestIdleSemantics``）。
4. **前端未交付（本卡零改动 `web/`）**：`grep -rn approval web/src` 与
   `grep -rn heartbeat web/src` 均零命中；
   `web/src/modules/chat/api/chatStream.ts` 的 `ChatStreamChunk.type` 联合类型仍是六值、
   `dispatchChunk` 的 `default` 分支对这两型**直接丢弃**。因此本文件只钉**后端帧契约**，
   浏览器端渲染另立排期（不在这条验收里冒充已完成）。

另外钉住三件事：`task_execution` 的两处必要放开（``TestTaskExecutionCallback``：async
回调与"闸门已被外部决策面批准"的恢复放行）、``--workers 2`` 下跨 worker 裁决靠回捞
T18 库行才看得见（``TestCrossWorkerDecisionLookup``）、兜底上限到期后那条 pending 的
**实际终态**是"被 ``gate.reject(by="in_loop_resume")`` 闭合"而不是"保持 PENDING"
（``TestTaskExecutionCallback::test_wait_cap_closes_pending_with_in_loop_attribution``）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from agentix.application.services.chat_service import ChatService
from agentix.application.services.stream_hitl import (
    APPROVAL_FRAME_TYPE,
    HEARTBEAT_FRAME_TYPE,
    StreamHitlChannel,
    build_stream_hitl,
)
from agentix.application.services.task_execution import run_task_execution
from agentix.config import settings
from agentix.policies.hitl.hitl_bridge import install_executor_hook, reset_hitl_bridge
from agentix.tools.gateway.human_review import (
    ReviewDecision,
    ReviewRequest,
    RiskLevel,
    clear_decision_surface,
    get_review_gate,
    mark_decision_surface,
    reset_review_gate,
)
from agentix.runtime.control_plane import reset_control_plane
from agentix.application.services.trajectory import reset_trajectory_recorder

REPO = Path(__file__).resolve().parents[1]

#: 审批等待用例的心跳间隔（远小于 STREAM_IDLE_TIMEOUT，便于在秒级内断言）
FAST_HEARTBEAT_S = 0.05


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_singletons():
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
    """三层 flag 全开 + 决策面已接线（生产装配的前置组合）。"""
    monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", True)
    monkeypatch.setattr(settings, "HITL_IN_LOOP_ENABLED", True)
    monkeypatch.setattr(settings, "STREAM_HITL_ENABLED", True)
    monkeypatch.setattr(settings, "STREAM_HITL_HEARTBEAT_S", FAST_HEARTBEAT_S)
    reset_review_gate()
    mark_decision_surface()
    yield
    reset_review_gate()


def _register_pending(request_id: str = "p174-gate-1") -> ReviewRequest:
    """把一条 PENDING 审批登记进闸门（等价执行点 ``preflight`` 的产物）。"""
    request = ReviewRequest(
        request_id=request_id,
        tool_name="write_task_artifact",
        risk_level=RiskLevel.HIGH,
        reason="review_required",
    )
    get_review_gate().restore_request(request)
    return request


# ---------------------------------------------------------------------------
# stub 图：可配置「事件 → 审批请求 → 事件」
# ---------------------------------------------------------------------------


class _StubStreamGraph:
    """按预设回放事件；可选在中间模拟 ``execute_task`` 的进环等待。

    ``approval`` 非空时，在 ``pre`` 事件之后从 config 取流内通道并 ``await
    channel.request(payload)``——这正是 ``lg_builder`` 注入壳 + ``task_execution``
    在生产里做的事（本层只验证帧与等待语义，节点细节由
    ``TestTaskExecutionCallback`` 覆盖）。
    """

    def __init__(
        self,
        *,
        pre: Optional[List[Dict[str, Any]]] = None,
        post: Optional[List[Dict[str, Any]]] = None,
        approval: Optional[Dict[str, Any]] = None,
        hang: bool = False,
    ) -> None:
        self._pre = pre or []
        self._post = post or []
        self._approval = approval
        self._hang = hang
        self.decision: Optional[str] = None
        self.seen_config: Optional[Dict[str, Any]] = None

    def astream_events(self, input_state, *, config=None, version=None, **kwargs):
        pre, post, approval, hang = (
            self._pre,
            self._post,
            self._approval,
            self._hang,
        )
        self.seen_config = config

        async def _gen():
            if hang:
                await asyncio.Event().wait()  # 永不产出（触发 idle 超时）
            for ev in pre:
                yield ev
            if approval is not None:
                channel = (config or {}).get("configurable", {}).get("stream_hitl")
                # flag-off 时注入壳不会给回调（节点侧拿不到 `hitl_in_loop_interrupt`）
                # ⇒ 这段"等裁决"根本不存在，事件序列与基线一致。
                if channel is not None:
                    self.decision = await channel.request(approval)
            for ev in post:
                yield ev

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


async def _collect(graph: _StubStreamGraph, session_id: str = "s-p174", **kwargs):
    service = ChatService(graph_getter=lambda: graph)
    return [ev async for ev in service.stream_events("问题", session_id, **kwargs)]


def _frames(frames: List[Dict[str, Any]], frame_type: str) -> List[Dict[str, Any]]:
    return [f for f in frames if f.get("type") == frame_type]


def _types(frames: List[Dict[str, Any]]) -> List[str]:
    return [str(f.get("type")) for f in frames]


async def _decide_later(request_id: str, delay: float = 0.05) -> None:
    """后台裁决（等价于运维在 ``/ops/hitl/approvals/{id}`` 上点了批准/拒绝）。"""
    await asyncio.sleep(delay)
    get_review_gate().approve(request_id, by="ops")


# ---------------------------------------------------------------------------
# 1. flag 治理与五处同步
# ---------------------------------------------------------------------------


class TestFlagSync:
    def test_defaults_are_off(self):
        fields = type(settings).model_fields
        assert fields["STREAM_HITL_ENABLED"].default is False
        assert fields["STREAM_HITL_HEARTBEAT_S"].default == 15.0

    def test_env_example_documents_keys(self):
        text = (REPO / ".env.example").read_text(encoding="utf-8")
        assert "STREAM_HITL_ENABLED=false" in text
        assert "STREAM_HITL_HEARTBEAT_S=15" in text

    def test_configuration_doc_documents_keys(self):
        text = (REPO / "docs/architecture/配置管理.md").read_text(encoding="utf-8")
        assert "| `STREAM_HITL_ENABLED` | `false`" in text
        assert "`STREAM_HITL_HEARTBEAT_S`" in text

    def test_selfcheck_allows_omission(self):
        from agentix.config.selfcheck import DEFAULT_IGNORED_MISSING

        assert "STREAM_HITL_ENABLED" in DEFAULT_IGNORED_MISSING
        assert "STREAM_HITL_HEARTBEAT_S" in DEFAULT_IGNORED_MISSING

    def test_p41_key_table_lists_keys(self):
        text = (REPO / "tests/test_p41_doc_consistency.py").read_text(encoding="utf-8")
        assert '"STREAM_HITL_ENABLED"' in text
        assert '"STREAM_HITL_HEARTBEAT_S"' in text

    def test_guardrail_doc_has_timeout_table(self):
        text = (REPO / "docs/architecture/运行时护栏.md").read_text(encoding="utf-8")
        assert "STREAM_HITL_ENABLED" in text
        assert "STREAM_IDLE_TIMEOUT" in text and "AGENT_TIMEOUT" in text
        assert "HITL_APPROVAL_DEADLINE_S" in text
        # 归因列只能是执行链真会产出的东西：§16 表行 ① 的归因是轨迹步名
        # stream_timeout（chat_service 记录），不是「配置项名 + 后缀」拼出来的幻影 token。
        section = text.split("## 16. ", 1)[1].split("\n## ", 1)[0]
        assert "stream_timeout" in section
        assert "stream_idle_timeout" not in section


# ---------------------------------------------------------------------------
# 2. 装配判定（唯一源 build_stream_hitl）
# ---------------------------------------------------------------------------


class TestBuildChannel:
    def test_flag_off_returns_none_and_leaves_config_untouched(self):
        config: Dict[str, Any] = {"configurable": {"thread_id": "t"}}
        before = dict(config["configurable"])
        assert build_stream_hitl(config) is None
        assert config["configurable"] == before, "flag-off 不得往 config 里加任何键"

    def test_parent_flags_gate_the_channel(self, monkeypatch):
        monkeypatch.setattr(settings, "STREAM_HITL_ENABLED", True)
        monkeypatch.setattr(settings, "HITL_IN_LOOP_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", False)
        config: Dict[str, Any] = {"configurable": {}}
        assert build_stream_hitl(config) is None
        assert config["configurable"] == {}

    def test_all_flags_on_injects_intent_and_channel(self, hitl_env):
        config: Dict[str, Any] = {"configurable": {"thread_id": "t"}}
        channel = build_stream_hitl(config)
        assert isinstance(channel, StreamHitlChannel)
        assert config["configurable"]["hitl_in_loop"] == 1
        assert config["configurable"]["stream_hitl"] is channel


# ---------------------------------------------------------------------------
# 3. 验收②：关 flag 时帧序列逐位一致
# ---------------------------------------------------------------------------


class TestFlagOffEquivalence:
    #: flag-off 的帧序列基线（与 A5 归一后的 wire 词汇一致：type 为唯一词汇）
    BASELINE_TYPES = ["metadata", "message", "done"]

    @pytest.mark.asyncio
    async def test_frames_identical_and_config_untouched(self):
        graph = _StubStreamGraph(
            pre=[_token_event("这是流式测试回答内容")],
            post=[_done_event()],
            # 图里带着"会请求审批"的分支——flag-off 时通道不存在，通道分支不会执行
            approval={"request_id": "never", "tool": "write_task_artifact"},
        )
        frames = await _collect(graph, session_id="s-p174-off")

        assert _types(frames) == self.BASELINE_TYPES
        assert not _frames(frames, APPROVAL_FRAME_TYPE)
        assert not _frames(frames, HEARTBEAT_FRAME_TYPE)
        configurable = graph.seen_config["configurable"]
        assert "stream_hitl" not in configurable
        assert "hitl_in_loop" not in configurable
        # 终帧仍带资料来源（帧形状不变）
        assert _frames(frames, "done")[0]["metadata"]["sources"]

    @pytest.mark.asyncio
    async def test_async_callback_not_injected_when_flag_off(self):
        """`lg_builder` 注入壳：flag-off 时 config 原样透传（不多 hitl 回调）。"""
        from agentix.orchestration.langgraph.lg_builder import (
            _wrap_execute_task_in_loop,
        )

        seen: Dict[str, Any] = {}

        async def _node(state, *, config):
            seen["config"] = config
            return {}

        wrapped = _wrap_execute_task_in_loop(_node)
        config = {"configurable": {"thread_id": "t"}}
        await wrapped({}, config=config)
        assert seen["config"] is config
        assert "hitl_in_loop_interrupt" not in config["configurable"]


# ---------------------------------------------------------------------------
# 4. 验收①③：审批帧 + 等待期心跳 + 裁决后继续
# ---------------------------------------------------------------------------


class TestApprovalFrames:
    @pytest.mark.asyncio
    async def test_approve_emits_frames_then_stream_continues(self, hitl_env):
        _register_pending("p174-approve")
        graph = _StubStreamGraph(
            pre=[_token_event("审批前")],
            approval={
                "request_id": "p174-approve",
                "tool": "write_task_artifact",
                "risk_level": "HIGH",
            },
            post=[_token_event("审批后"), _done_event()],
        )
        decider = asyncio.ensure_future(_decide_later("p174-approve", 0.1))
        frames = await _collect(graph, session_id="s-p174-approve")
        await decider

        approvals = _frames(frames, APPROVAL_FRAME_TYPE)
        assert [a["metadata"]["status"] for a in approvals] == ["pending", "decided"]
        assert approvals[0]["metadata"]["request_id"] == "p174-approve"
        assert approvals[0]["metadata"]["tool"] == "write_task_artifact"
        assert approvals[1]["metadata"]["decision"] == "approve"
        # 裁决点在前（图继续）——审批帧必须在 done 之前
        assert _types(frames)[-1] == "done"
        assert graph.decision == "approve"
        # 裁决后流继续：审批前后的 token 都上屏
        content = "".join(str(f.get("content") or "") for f in frames if f["type"] == "message")
        assert content == "审批前审批后"

    @pytest.mark.asyncio
    async def test_reject_terminates_at_governance_terminal(self, hitl_env):
        _register_pending("p174-reject")
        graph = _StubStreamGraph(
            approval={
                "request_id": "p174-reject",
                "tool": "write_task_artifact",
                "risk_level": "HIGH",
            },
            post=[_done_event()],
        )

        async def _reject_later() -> None:
            await asyncio.sleep(0.1)
            get_review_gate().reject("p174-reject", by="ops", note="不批")

        decider = asyncio.ensure_future(_reject_later())
        frames = await _collect(graph, session_id="s-p174-reject")
        await decider

        assert graph.decision == "reject"
        approvals = _frames(frames, APPROVAL_FRAME_TYPE)
        assert approvals[-1]["metadata"]["decision"] == "reject"
        # 治理层终局：流照常以 done 收口（不是 error 帧——拒绝是判定，不是故障）
        assert _types(frames)[-1] == "done"
        assert not _frames(frames, "error")

    @pytest.mark.asyncio
    async def test_decide_through_bridge_carries_sidecar_id(self, hitl_env):
        """裁决入口是 ``/ops/hitl/approvals/{sidecar_id}``：帧里必须带得出该 id。"""
        from agentix.policies.hitl.hitl_bridge import get_hitl_bridge
        from agentix.policies.hitl.sidecar import ReviewDecisionAction

        install_executor_hook()
        bridge = get_hitl_bridge()
        gate_request = _register_pending("p174-bridge")
        suspend = await bridge.open_from_gate(gate_request, {"name": "p174.md"})
        assert suspend.sidecar_request_id, "硬边界：桥必须给出 sidecar id"
        assert bridge.sidecar_id_for_gate("p174-bridge") == suspend.sidecar_request_id

        graph = _StubStreamGraph(
            approval={
                "request_id": "p174-bridge",
                "tool": "write_task_artifact",
                "risk_level": "HIGH",
            },
            post=[_done_event()],
        )

        async def _decide_via_bridge() -> None:
            await asyncio.sleep(0.1)
            decision = await bridge.decide(
                suspend.sidecar_request_id,
                ReviewDecisionAction.APPROVE,
                by="ops",
            )
            assert decision.approved

        decider = asyncio.ensure_future(_decide_via_bridge())
        frames = await _collect(graph, session_id="s-p174-bridge")
        await decider

        pending_frame = _frames(frames, APPROVAL_FRAME_TYPE)[0]
        assert pending_frame["metadata"]["sidecar_request_id"] == suspend.sidecar_request_id
        assert suspend.sidecar_request_id in pending_frame["metadata"]["decide_url"]
        assert graph.decision == "approve"


# ---------------------------------------------------------------------------
# 5. 验收③：等待不冻结心跳，静默仍按 idle 判
# ---------------------------------------------------------------------------


class TestIdleSemantics:
    @pytest.mark.asyncio
    async def test_approval_wait_beyond_idle_timeout_is_not_idle(self, hitl_env, monkeypatch):
        monkeypatch.setattr(settings, "STREAM_IDLE_TIMEOUT", 0.2)
        monkeypatch.setattr(settings, "STREAM_HITL_HEARTBEAT_S", 0.05)
        _register_pending("p174-wait")
        graph = _StubStreamGraph(
            approval={
                "request_id": "p174-wait",
                "tool": "write_task_artifact",
                "risk_level": "HIGH",
            },
            post=[_done_event()],
        )
        decider = asyncio.ensure_future(_decide_later("p174-wait", 0.8))
        frames = await _collect(graph, session_id="s-p174-wait")
        await decider

        assert not _frames(frames, "error"), "审批等待期不得被 idle 看门狗判超时"
        heartbeats = _frames(frames, HEARTBEAT_FRAME_TYPE)
        assert len(heartbeats) >= 3, f"等待期必须持续发心跳，实得 {len(heartbeats)}"
        assert heartbeats[0]["metadata"]["status"] == "waiting_approval"
        assert _types(frames)[-1] == "done"

    @pytest.mark.asyncio
    async def test_silence_without_approval_still_times_out(self, hitl_env, monkeypatch):
        monkeypatch.setattr(settings, "STREAM_IDLE_TIMEOUT", 0.1)
        graph = _StubStreamGraph(hang=True)
        frames = await _collect(graph, session_id="s-p174-idle")

        assert _frames(frames, "error"), "无审批的静默必须仍按 STREAM_IDLE_TIMEOUT 收口"
        assert not _frames(frames, HEARTBEAT_FRAME_TYPE)
        assert _types(frames)[-1] == "done"  # 恰好一次 done 契约不破

    @pytest.mark.asyncio
    async def test_wall_clock_still_bounds_approval_wait(self, hitl_env, monkeypatch):
        """墙钟（AGENT_TIMEOUT）仍管着审批等待：心跳让等待活着，但不豁免墙钟。"""
        monkeypatch.setattr(settings, "STREAM_HITL_HEARTBEAT_S", 0.05)
        monkeypatch.setattr(settings, "AGENT_TIMEOUT", 0)
        _register_pending("p174-wall")
        graph = _StubStreamGraph(
            approval={"request_id": "p174-wall", "tool": "write_task_artifact"},
            post=[_done_event()],
        )
        frames = await _collect(graph, session_id="s-p174-wall")

        assert _frames(frames, "error"), "墙钟超限必须 error 收口"
        assert _types(frames)[-1] == "done"


# ---------------------------------------------------------------------------
# 6. task_execution 侧的两处必要放开
# ---------------------------------------------------------------------------


def _steps(name: str = "p174.md") -> list:
    return [
        {
            "id": "s1",
            "tool": "write_task_artifact",
            "args": {"name": name, "content": "T23 正文", "kind": "report"},
            "depends_on": [],
            "rationale": "高风险写（流内审批探针）",
        }
    ]


class _ChannelPump:
    """模拟流侧循环：持续 ``next_item`` 把审批帧/心跳帧抽出来（生产里由
    ``chat_service`` 的取事件循环干这件事）。

    通道是被动的一方——没有流侧泵，节点侧 ``request`` 就永远等不到裁决。
    本类就是那条泵，顺带把帧收起来做断言。
    """

    def __init__(self, channel: StreamHitlChannel) -> None:
        self.channel = channel
        self.frames: List[Dict[str, Any]] = []
        self._task: Optional["asyncio.Future[None]"] = None

    def start(self) -> "_ChannelPump":
        self._task = asyncio.ensure_future(self._run())
        return self

    async def _run(self) -> None:
        async def _anext() -> Any:
            await asyncio.Event().wait()  # 本测试没有图事件要取

        try:
            while True:
                try:
                    _event, frame = await self.channel.next_item(_anext)
                except StopAsyncIteration:
                    return
                if frame is not None:
                    self.frames.append(frame)
        except asyncio.CancelledError:
            raise

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.channel.close()

    def of(self, frame_type: str) -> List[Dict[str, Any]]:
        return [f for f in self.frames if f.get("type") == frame_type]


class _FakePendingStore:
    """T18 库面的鸭子型替身：``hitl_bridge`` 对 store 只要求 ``load`` 这一个读法。

    ``raises=True`` 用来模拟"库不可用"，验证回捞失败是 fail-open（纪律 2）。
    """

    def __init__(self, status: str = "approved", *, raises: bool = False) -> None:
        self.status = status
        self.raises = raises
        self.calls: List[str] = []

    def load(self, sidecar_request_id: str) -> Optional[Dict[str, Any]]:
        self.calls.append(sidecar_request_id)
        if self.raises:
            raise RuntimeError("pending store unavailable (test double)")
        return {"sidecar_request_id": sidecar_request_id, "status": self.status}


class TestCrossWorkerDecisionLookup:
    """验收①在 ``--workers 2`` 下成立：裁决落在**另一个** worker 时靠回捞库行看终态。

    默认部署是 ``--workers 2``（``docker-compose.yml``）。``bridge.decide`` 只写
    **裁决方进程**的闸门，等流所在的进程看到的仍是 ``PENDING``；只读本地闸门会让
    这条流一直等到 ``AGENT_TIMEOUT`` 以"处理请求超时"收口——那是超时，不是治理终局。
    """

    def test_store_status_map_matches_t18_status_vocabulary(self):
        """回捞用的「库行终态 → 裁决」映射必须与 T18 的状态词表同源（一个判定一个源）。"""
        from agentix.application.services.stream_hitl import _STORE_STATUS_DECISION
        from agentix.infrastructure.persistence.hitl_pending_store import DECIDED_STATUSES

        assert set(_STORE_STATUS_DECISION) == set(DECIDED_STATUSES), (
            "映射键集漂移：T18 的状态词表是唯一真源，增删状态必须同批改这里"
        )
        assert set(_STORE_STATUS_DECISION.values()) == {"approve", "reject"}

    @pytest.mark.asyncio
    async def test_pending_falls_back_to_store_terminal_state(self, hitl_env, monkeypatch):
        from agentix.policies.hitl import hitl_bridge as bridge_module
        from agentix.policies.hitl.hitl_bridge import get_hitl_bridge

        store = _FakePendingStore("approved")
        # 装配判据唯一源就是这一个函数（不在 stream_hitl 里另读 flag）
        monkeypatch.setattr(bridge_module, "resolve_pending_store", lambda: store)

        gate_request = _register_pending("p174-xworker")
        suspend = await get_hitl_bridge().open_from_gate(
            gate_request, {"name": "p174-xworker.md"}
        )
        assert suspend.sidecar_request_id, "硬边界：桥必须给出 sidecar id"

        channel = StreamHitlChannel(heartbeat_s=FAST_HEARTBEAT_S, poll_s=0.01, idle_timeout=5.0)
        pump = _ChannelPump(channel).start()
        try:
            decision = await asyncio.wait_for(
                channel.request({"request_id": "p174-xworker", "tool": "write_task_artifact"}),
                timeout=5.0,
            )
        finally:
            await pump.stop()

        assert decision == "approve", "库行已是 approved ⇒ 等流必须按该裁决放行"
        assert store.calls and store.calls[-1] == suspend.sidecar_request_id, (
            "回捞必须按 sidecar_request_id 读同一条库记录"
        )
        # 本地闸门仍是 PENDING：跨 worker 的裁决根本没落在这个进程——这正是要回捞的理由
        assert get_review_gate().get("p174-xworker").decision is ReviewDecision.PENDING
        assert pump.of(APPROVAL_FRAME_TYPE)[-1]["metadata"]["decision"] == "approve"

    @pytest.mark.asyncio
    async def test_store_failure_degrades_to_local_polling(self, hitl_env, monkeypatch):
        """回捞失败只当"未命中"：不抛进取事件循环、不提前收口，本地闸门照旧收口。"""
        from agentix.policies.hitl import hitl_bridge as bridge_module
        from agentix.policies.hitl.hitl_bridge import get_hitl_bridge

        store = _FakePendingStore("approved", raises=True)
        monkeypatch.setattr(bridge_module, "resolve_pending_store", lambda: store)

        gate_request = _register_pending("p174-xworker-down")
        assert await get_hitl_bridge().open_from_gate(gate_request, {"name": "x.md"})

        channel = StreamHitlChannel(heartbeat_s=FAST_HEARTBEAT_S, poll_s=0.01, idle_timeout=5.0)
        pump = _ChannelPump(channel).start()

        async def _reject_locally_later() -> None:
            await asyncio.sleep(0.2)
            assert get_review_gate().reject("p174-xworker-down", by="ops") is True

        decider = asyncio.ensure_future(_reject_locally_later())
        try:
            decision = await asyncio.wait_for(
                channel.request(
                    {"request_id": "p174-xworker-down", "tool": "write_task_artifact"}
                ),
                timeout=5.0,
            )
        finally:
            await decider
            await pump.stop()

        assert store.calls, "回捞失败也必须真的试过（不静默跳过）"
        assert decision == "reject", "回捞失败只降级为未命中，本地闸门照旧轮询收口"


class TestTaskExecutionCallback:
    """流内回调是 async 的；且裁决经桥落在闸门上后，恢复不得被"重复批准"卡死。"""

    @pytest.fixture
    def loop_env(self, hitl_env, monkeypatch):
        from agentix.application.services.artifact_store import reset_artifact_store
        from agentix.application.services.task_tools_bridge import (
            install_task_write_tools,
        )
        from agentix.tools import registry_init
        from agentix.tools.definitions import registry as tool_registry
        from agentix.tools.gateway.executor import reset_tool_executor

        monkeypatch.setattr(settings, "TASK_EXEC_ENABLED", True)
        registry_init._DECLARATIONS.clear()
        reset_tool_executor()
        reset_artifact_store()
        install_task_write_tools()
        _LAST_REQUEST_ID.clear()
        yield
        registry_init._DECLARATIONS.clear()
        for tool in ("append_task_note", "write_task_artifact", "read_artifact"):
            tool_registry.delete_tool(tool)
        reset_tool_executor()
        reset_artifact_store()
        _LAST_REQUEST_ID.clear()

    @pytest.mark.asyncio
    async def test_async_callback_approve_retries_once(self, loop_env):
        channel = StreamHitlChannel(
            heartbeat_s=FAST_HEARTBEAT_S,
            poll_s=0.01,
            idle_timeout=5.0,
        )
        pump = _ChannelPump(channel).start()
        decider = asyncio.ensure_future(_decide_when_pending(approve=True))
        try:
            result = await run_task_execution(
                "产出报告（流内批准）",
                session_id="p174-stream-approve",
                task_steps=_steps("p174-stream-approve.md"),
                review_interrupt=_approver(channel),
            )
        finally:
            await decider
            await pump.stop()

        assert result.terminal != "failed", result.reason
        from agentix.application.services.artifact_store import get_artifact_store

        assert len(get_artifact_store()._items) == 1, "批准后重试必须真的执行一次"
        approval_frames = pump.of(APPROVAL_FRAME_TYPE)
        assert [f["metadata"]["status"] for f in approval_frames] == ["pending", "decided"]
        assert approval_frames[-1]["metadata"]["decision"] == "approve"

    @pytest.mark.asyncio
    async def test_async_callback_reject_is_terminal(self, loop_env):
        channel = StreamHitlChannel(
            heartbeat_s=FAST_HEARTBEAT_S,
            poll_s=0.01,
            idle_timeout=5.0,
        )
        pump = _ChannelPump(channel).start()
        decider = asyncio.ensure_future(_decide_when_pending(approve=False))
        try:
            result = await run_task_execution(
                "产出报告（流内拒绝）",
                session_id="p174-stream-reject",
                task_steps=_steps("p174-stream-reject.md"),
                review_interrupt=_approver(channel),
            )
        finally:
            await decider
            await pump.stop()

        from agentix.application.services.artifact_store import get_artifact_store

        assert result.terminal == "failed"
        assert "in_loop_review_rejected" in result.reason
        assert len(get_artifact_store()._items) == 0
        assert pump.of(APPROVAL_FRAME_TYPE)[-1]["metadata"]["decision"] == "reject"

    @pytest.mark.asyncio
    async def test_wait_cap_closes_pending_with_in_loop_attribution(self, loop_env):
        """兜底上限到期：cap 只交出 ``reject``，**闭合**那条 pending 的是节点的拒绝分支。

        钉的是事实口径（§16 / CHANGELOG / ``stream_hitl`` docstring 三处同一句）：
        ``gate.reject(by="in_loop_resume", note="in-loop reject")`` 把该记录**闭合**为
        ``REJECTED``，归因是"流内等待上限触发的拒绝"；「保持 PENDING」的旧写法与代码相反。
        与 T19 的到期拒（``decided_by=system:deadline`` + ``decision_reason=
        auto_reject_timeout``）是两条不同来源，靠 ``decided_by`` / ``note`` 可区分。
        """
        channel = StreamHitlChannel(
            heartbeat_s=FAST_HEARTBEAT_S,
            poll_s=0.01,
            idle_timeout=5.0,
            wait_cap_s=0.2,
        )
        pump = _ChannelPump(channel).start()
        try:
            result = await run_task_execution(
                "产出报告（流内等待上限）",
                session_id="p174-stream-cap",
                task_steps=_steps("p174-stream-cap.md"),
                review_interrupt=_approver(channel),
            )
        finally:
            await pump.stop()

        request_id = _current_request_id()
        assert request_id, "节点必须真的挂起过（cap 才有收口对象）"
        record = get_review_gate().get(request_id)
        assert record is not None
        assert record.decision is ReviewDecision.REJECTED, (
            "cap 到期后闸门记录必须是终态 REJECTED（『保持 PENDING』的旧文档与代码相反）"
        )
        assert record.decided_by == "in_loop_resume", "归因只能是流内等待上限触发的拒绝"
        assert record.note == "in-loop reject"
        # 与 T19 的到期自动拒绝可区分（不同来源，不许混写成一个归因）
        assert record.decided_by != "system:deadline"
        assert result.terminal == "failed"
        assert "in_loop_review_rejected" in result.reason
        assert pump.of(APPROVAL_FRAME_TYPE)[-1]["metadata"]["decision"] == "reject"


#: 最近一次进环回调看到的闸门请求 id（用例用来在后台"裁决"）
_LAST_REQUEST_ID: Dict[str, str] = {}


def _current_request_id() -> str:
    return _LAST_REQUEST_ID.get("request_id", "")


async def _decide_when_pending(*, approve: bool, timeout_s: float = 5.0) -> None:
    """等节点真的挂起（回调已记下 request_id）再裁决，避免"按点还没到就放枪"。"""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not _current_request_id():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("进环回调始终没有拿到 request_id（节点没挂起？）")
        await asyncio.sleep(0.01)
    request_id = _current_request_id()
    gate = get_review_gate()
    if approve:
        assert gate.approve(request_id, by="ops") is True
    else:
        assert gate.reject(request_id, by="ops") is True


def _approver(channel: StreamHitlChannel):
    """把通道 ``request`` 包一层：记下 request_id 再交给通道（后台裁决用）。"""

    async def _callback(payload: Dict[str, Any]) -> str:
        _LAST_REQUEST_ID["request_id"] = str(payload.get("request_id") or "")
        return await channel.request(payload)

    return _callback


class TestGateAlreadyApprovedRecovery:
    """闸门已是 APPROVED（外部决策面先落账）时，恢复不得被判成"批准未落账"。"""

    @pytest.mark.asyncio
    async def test_already_approved_is_recorded(self, monkeypatch):
        from agentix.tools.gateway.human_review import ReviewDecision as _RD

        assert _RD.APPROVED.value == "approved"
        from agentix.application.services.task_execution import _gate_already_approved

        request = ReviewRequest(
            request_id="p174-already",
            tool_name="write_task_artifact",
            risk_level=RiskLevel.HIGH,
            reason="review_required",
        )
        gate = get_review_gate()
        gate.restore_request(request)
        assert _gate_already_approved(gate, "p174-already") is False
        assert gate.approve("p174-already", by="ops") is True
        assert _gate_already_approved(gate, "p174-already") is True
        # 第二次批准在闸门侧不落账（仍是幂等 False），但上面的读判据照样认得
        assert gate.approve("p174-already", by="ops") is False
        assert _gate_already_approved(gate, "p174-already") is True
