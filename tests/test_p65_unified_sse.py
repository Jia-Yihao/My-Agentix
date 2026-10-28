"""A5 SSE 全量统一批次（p65+）——R5a run-scoped emitter / R5b 词汇归一 / R5d 流式轨迹通电与持久化评估门。

覆盖（对应 docs/migration/架构重构_统一执行链方案_v2.md Stage A · A5，L113）：

1. **R5a + 守卫 #5**（v2 §6「SSE event 必须来自统一 Run/Event stream」在此
   测试化）：chat 与 retrieval 两条流的每帧都带 ``seq``（本 Run 单调从 1）
   与 ``run_id``（== ControlPlane 登记的 Run）；登记故障降级为「有 seq 无
   run_id」且不阻断事件流；impl 直驱（不经 wrapper）不戳记——戳记单点在
   ``stream_events`` 投影层。
2. **R5b 词汇归一**：线上帧以 ``type`` 为唯一词汇（不带 ``event``）；
   ``_to_sse`` 序列化同样不产出 ``event`` 键（前端两 parser 已容错两词，
   见 web/src/api/sse.ts 与各模块 dispatch）。
3. **R5d 轨迹通电**：流式失败（异常/idle 超时）补记 ``error`` 步；成功
   补记 ``completion_validator`` 步（失败路径不记 completion）；工具步照记。
4. **R5d 持久化评估门**：``TRAJECTORY_SINK=jsonl`` 下流式失败轨迹落盘，
   ``export_trajectory_packages`` 可导出含 ``first_error_step_index`` 的
   轨迹包（evals/trajectory_regression.py 的消费契约）。

R5c（前端共 parser）的载体在 ``web/src/api/sse.ts`` + vitest（126 绿）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest

from agentix.application.services.chat_service import ChatService
from agentix.runtime.control_plane import (
    get_control_plane,
    reset_control_plane,
)
from agentix.application.services.trajectory import (
    reset_trajectory_recorder,
)


@pytest.fixture(autouse=True)
def _fresh_plane():
    """每个用例独立的 ControlPlane 单例（防运行记录跨用例串扰）。"""
    reset_control_plane()
    yield
    reset_control_plane()


@pytest.fixture(autouse=True)
def _fresh_trajectory():
    """每个用例独立的轨迹记录器单例（默认 memory sink；防跨用例污染）。"""
    reset_trajectory_recorder()
    yield
    reset_trajectory_recorder()


# ---------------------------------------------------------------------------
# 流夹具（chat 侧：mock graph，零 LLM/Docker）
# ---------------------------------------------------------------------------


class _FakeChunk:
    def __init__(self, content: Any) -> None:
        self.content = content


class _StubStreamGraph:
    """按顺序回放预设 LangGraph 事件；可选回放后抛异常 / 永久挂起。"""

    def __init__(
        self,
        events: Optional[List[Dict[str, Any]]] = None,
        *,
        exc: Optional[BaseException] = None,
        hang: bool = False,
    ):
        self._events = events or []
        self._exc = exc
        self._hang = hang

    def astream_events(self, input_state, *, config=None, version=None):
        events, exc, hang = self._events, self._exc, self._hang

        async def _gen():
            if hang:
                await asyncio.Event().wait()  # 永不产出（触发 idle 超时）
            for ev in events:
                yield ev
            if exc is not None:
                raise exc

        return _gen()


def _ev(
    kind: str,
    name: Optional[str] = None,
    data: Optional[Dict] = None,
    metadata: Optional[Dict] = None,
) -> Dict[str, Any]:
    return {"event": kind, "name": name, "data": data or {}, "metadata": metadata or {}}


def _success_events() -> List[Dict[str, Any]]:
    """路由 → 工具开始/结束 → ≥6 字 token → 最外层图结束（含 sources）。"""
    answer = "这是流式测试回答内容"
    return [
        _ev(
            "on_chain_end",
            name="analyze_and_route_query",
            data={"output": {"router": {"type": "legal", "logic": "kb-query"}}},
            metadata={"langgraph_node": "analyze_and_route_query"},
        ),
        _ev("on_tool_start", name="milvus_search"),
        _ev(
            "on_chat_model_stream",
            name="ChatModel",
            data={"chunk": _FakeChunk(answer)},
            metadata={"langgraph_node": "create_kb_query"},
        ),
        _ev("on_tool_end", name="milvus_search"),
        {
            "event": "on_chain_end",
            "name": "LangGraph",
            "data": {"output": {"sources": [{"document_id": "d1", "source": "d1"}]}},
            "metadata": {},
        },
    ]


async def _collect_chat(
    events: Optional[List[Dict[str, Any]]] = None,
    *,
    exc: Optional[BaseException] = None,
    session_id: str = "s-p65",
) -> List[Dict[str, Any]]:
    """经 ``stream_events`` 投影层（含 run-scoped emitter 戳记）收集全部帧。"""
    graph = _StubStreamGraph(events if events is not None else _success_events(), exc=exc)
    service = ChatService(graph_getter=lambda: graph)
    return [ev async for ev in service.stream_events("问题", session_id)]


def _stream_runs() -> List[Any]:
    return [r for r in get_control_plane().list_runs() if r.meta.get("channel") == "stream"]


def _all_trajectory_steps() -> List[Any]:
    """扫出全局记录器里所有 trace 的步骤（trace id 由 request_scope 决定）。"""
    from agentix.application.services.trajectory import get_trajectory_recorder

    rec = get_trajectory_recorder()
    steps: List[Any] = []
    for tid in rec.trace_ids():
        steps.extend(rec.get(tid))
    return steps


# ---------------------------------------------------------------------------
# R5a + 守卫 #5：run-scoped emitter
# ---------------------------------------------------------------------------


class TestRunScopedEmitter:
    @pytest.mark.asyncio
    async def test_chat_frames_carry_seq_and_run_id(self):
        """守卫 #5（chat 侧）：每帧 seq 单调从 1 起、run_id == 登记的 Run。"""
        frames = await _collect_chat(session_id="s-p65-emitter")

        assert frames, "流必须产出帧"
        assert [f["seq"] for f in frames] == list(range(1, len(frames) + 1)), (
            "seq 必须本 Run 内单调从 1 起"
        )
        run_ids = {f["run_id"] for f in frames}
        assert len(run_ids) == 1 and "" not in run_ids, f"run_id 必须唯一且非空: {run_ids}"

        runs = _stream_runs()
        assert len(runs) == 1
        assert runs[0].run_id in run_ids, "帧的 run_id 必须指向 ControlPlane 登记的 Run"
        assert runs[0].state.value == "succeeded"

    @pytest.mark.asyncio
    async def test_registration_failure_degrades_to_seq_without_run_id(
        self, monkeypatch
    ):
        """登记故障降级：不阻断事件流；帧带 seq、不伪造 run_id。"""

        def _boom(*args, **kwargs):
            raise RuntimeError("plane offline")

        monkeypatch.setattr(type(get_control_plane()), "spawn", _boom)

        frames = await _collect_chat(session_id="s-p65-degrade")

        assert any(f["type"] == "done" for f in frames), "降级不得阻断事件流"
        assert [f["seq"] for f in frames] == list(range(1, len(frames) + 1))
        assert all("run_id" not in f for f in frames), "登记失败不得伪造 run_id"

    @pytest.mark.asyncio
    async def test_impl_without_run_scope_is_unstamped(self):
        """戳记单点在 ``stream_events`` 投影层；impl 直驱（A1 测试口径）不戳记。"""
        graph = _StubStreamGraph(_success_events())
        service = ChatService(graph_getter=lambda: graph)
        frames = [ev async for ev in service._stream_events_impl("问题", "s-p65-impl")]

        assert frames
        assert all("seq" not in f and "run_id" not in f for f in frames)


# ---------------------------------------------------------------------------
# R5a：retrieval 流入控制面 + 帧戳记
# ---------------------------------------------------------------------------


class _FakeMsg:
    id = 42


def _patch_retrieval_stream(
    monkeypatch,
    *,
    prepared: Any = None,
    final_frames: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """把 retrieval 流内部依赖替换为离线桩（与 test_retrieval_progress_events 同款）。"""
    from agentix.retrieval.settings import settings as retrieval_settings
    from agentix.retrieval.usecases import chat_service as cs
    from agentix.retrieval.usecases import chat_service_helpers as helpers

    # 流式轨迹观测（retrieval 自有 recorder）不参与本批断言；关掉避免失败落盘
    monkeypatch.setattr(retrieval_settings, "TRAJECTORY_ENABLED", False)

    monkeypatch.setattr(
        cs,
        "_prepare_conversation_and_scope",
        lambda *a, **kw: (
            prepared if prepared is not None else ("cid-1", _FakeMsg(), "kb", "kb-1")
        ),
    )
    monkeypatch.setattr(
        cs, "_ensure_kb_ready_or_reject", lambda *a, **kw: (object(), None, None, None)
    )
    monkeypatch.setattr(cs, "count_documents_in_scope", lambda *a, **kw: 3)

    async def fake_prepare_context(*a, **kw):
        return {"history_text": "", "scope_label": ""}

    async def fake_preflight(*a, **kw):
        return None  # None = 继续主链路

    default_final = final_frames or [
        {
            "type": "answer_chunk",
            "content": "这是流式测试回答内容",
            "request_id": "req-p65",
            "finished": False,
        },
        {
            "type": "answer_chunk",
            "content": "",
            "request_id": "req-p65",
            "finished": True,
            "conversation_id": "cid-1",
            "message_id": 1,
            "answer_id": 2,
            "citations": [],
        },
    ]

    async def fake_final(*a, **kw):
        for frame in default_final:
            yield frame

    monkeypatch.setattr(helpers, "_prepare_context", fake_prepare_context)
    monkeypatch.setattr(helpers, "_stream_retrieval_preflight", fake_preflight)
    monkeypatch.setattr(helpers, "_stream_final_answer", fake_final)


async def _collect_retrieval(**kwargs: Any) -> List[Dict[str, Any]]:
    from agentix.retrieval.usecases import chat_service as cs

    return [
        p
        async for p in cs._stream_answer_impl(
            object(),
            kb_id="kb-1",
            question="q",
            conversation_id=None,
            request_id="req-p65",
            user_id="u-1",
            cache_redis=None,
            **kwargs,
        )
    ]


class TestRetrievalRunRegistration:
    @pytest.mark.asyncio
    async def test_retrieval_stream_registers_run_and_stamps_frames(self, monkeypatch):
        """守卫 #5（retrieval 侧）：0 Run 登记缺口补齐，帧带 seq/run_id。"""
        _patch_retrieval_stream(monkeypatch)
        frames = await _collect_retrieval()

        assert frames
        assert [f["seq"] for f in frames] == list(range(1, len(frames) + 1))
        run_ids = {f["run_id"] for f in frames}
        assert len(run_ids) == 1 and "" not in run_ids

        runs = _stream_runs()
        assert len(runs) == 1
        run = runs[0]
        assert run.run_id in run_ids
        assert run.agent_name == "retrieval_chat"
        assert run.meta.get("domain") == "retrieval"
        assert run.state.value == "succeeded"

    @pytest.mark.asyncio
    async def test_retrieval_reject_frame_marks_run_failed(self, monkeypatch):
        """拒答帧（error）→ Run FAILED（error 码进运行记录），帧照常戳记。"""
        _patch_retrieval_stream(
            monkeypatch,
            prepared={
                "type": "error",
                "code": "CONVERSATION_NOT_FOUND",
                "content": "会话不存在",
            },
        )
        frames = await _collect_retrieval()

        assert any(f["type"] == "error" for f in frames)
        assert all("seq" in f and "run_id" in f for f in frames)

        runs = _stream_runs()
        assert len(runs) == 1
        assert runs[0].state.value == "failed"
        assert runs[0].error == "CONVERSATION_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_retrieval_disconnect_marks_cancelled(self, monkeypatch):
        """首帧后 aclose（客户端断连）→ Run CANCELLED(client_disconnected)。"""
        _patch_retrieval_stream(monkeypatch)
        from agentix.retrieval.usecases import chat_service as cs

        gen = cs._stream_answer_impl(
            object(),
            kb_id="kb-1",
            question="q",
            conversation_id=None,
            request_id="req-p65-disc",
            user_id="u-1",
            cache_redis=None,
        )
        first = await gen.__anext__()
        assert "seq" in first and "run_id" in first
        await gen.aclose()

        runs = _stream_runs()
        assert len(runs) == 1
        assert runs[0].state.value == "cancelled"
        assert runs[0].error == "client_disconnected"


# ---------------------------------------------------------------------------
# R5b：词汇归一（type 为唯一词汇）
# ---------------------------------------------------------------------------


class TestSingleVocabulary:
    @pytest.mark.asyncio
    async def test_chat_frames_type_only(self):
        frames = await _collect_chat()
        for f in frames:
            assert "type" in f, f"帧缺 type: {f}"
            assert "event" not in f, f"A5 归一后帧不得携带 event 词: {f}"

    @pytest.mark.asyncio
    async def test_retrieval_frames_type_only(self, monkeypatch):
        _patch_retrieval_stream(monkeypatch)
        frames = await _collect_retrieval()
        assert frames
        for f in frames:
            assert "type" in f, f"帧缺 type: {f}"
            assert "event" not in f, f"A5 归一后帧不得携带 event 词: {f}"

    @pytest.mark.asyncio
    async def test_to_sse_serializes_type_seq_run_id_without_event(self):
        """HTTP 投影（DTO 序列化）：type/seq/run_id 在场、event 不在场。"""
        from agentix.interfaces.http.v1.chat import _to_sse

        line = _to_sse(
            {
                "type": "message",
                "content": "hi",
                "session_id": "s1",
                "seq": 3,
                "run_id": "r-1",
            }
        )
        assert line.startswith("data: ") and line.endswith("\n\n")
        payload = json.loads(line[len("data: ") :])
        assert payload["type"] == "message"
        assert payload["seq"] == 3
        assert payload["run_id"] == "r-1"
        assert "event" not in payload, "A5 归一后 wire 不得出现 event 键"

    def test_to_sse_carries_terminal_perf_fields_and_nothing_else(self):
        """回归守卫（2026-10-23 宿主实跑发现）：emitter 的终帧耗时必须出得来。

        断链现场：RunEventEmitter 已在终帧 setdefault 了 duration_ms / ttft_ms /
        breakdown_ms，`_to_sse` 却按 ChatStreamChunk 的字段白名单序列化，三个键整个
        被丢弃 ⇒ 前端 `web/src/modules/knowledge/api/chatStreamHandlers.ts` 的耗时芯片
        永远拿不到数据，而它自己的单测喂的是手写假帧，掩盖了这条断链。
        两头都钉：终帧必须带，非终帧连 null 占位键都不许多。
        """
        from agentix.interfaces.http.v1.chat import _PERF_FRAME_KEYS, _to_sse

        def payload(line):
            return json.loads(line[len("data: ") :])

        terminal = payload(
            _to_sse(
                {
                    "type": "done",
                    "seq": 4,
                    "run_id": "r-1",
                    "duration_ms": 2451,
                    "ttft_ms": 2010,
                    "breakdown_ms": {"stream_ms": 2451, "ttft_ms": 2010},
                }
            )
        )
        assert {k: terminal[k] for k in _PERF_FRAME_KEYS} == {
            "duration_ms": 2451,
            "ttft_ms": 2010,
            "breakdown_ms": {"stream_ms": 2451, "ttft_ms": 2010},
        }

        content = payload(_to_sse({"type": "message", "content": "hi", "seq": 2}))
        assert not set(content) & set(_PERF_FRAME_KEYS), "非终帧不得出现性能键（也不得补 null）"

    @pytest.mark.asyncio
    async def test_emitter_terminal_fields_survive_to_wire(self):
        """整链守卫：stub 图 → stream_events（emitter 戳记）→ `_to_sse` → wire。

        只测 DTO 会漏掉今天这种断链（emitter 内帧有 duration_ms，wire 上没有），
        所以这条跑真实投影链，并在 emitter 侧先确认字段确实被注入了。
        """
        from agentix.interfaces.http.v1.chat import _to_sse

        frames = await _collect_chat()
        internal_done = [f for f in frames if f.get("type") == "done"]
        assert internal_done and "duration_ms" in internal_done[-1], (
            f"emitter 未注入终帧耗时，本条守卫失去意义：{sorted(internal_done[-1] if internal_done else {})}"
        )

        wire = [json.loads(_to_sse(f)[len("data: ") :]) for f in frames]
        terminal = [w for w in wire if w["type"] == "done"]
        assert terminal, f"stub 链未产出 done 终帧：{[w['type'] for w in wire]}"
        for key in ("duration_ms", "ttft_ms"):
            assert key in terminal[-1], f"终帧 {key} 在序列化时被丢弃：{sorted(terminal[-1])}"


# ---------------------------------------------------------------------------
# 路由帧：必须按 router 节点的**真实返回形状**测
# ---------------------------------------------------------------------------


class TestRouteFrameUsesRealNodeShape:
    """``route`` 必须真的出现在帧上。

    案发背景（2026-10-23 拉起真栈实测）：SSE 每一帧的 ``route`` 都是 ``null``。根因是
    ``_stream_events_impl`` 用 ``isinstance(router_data, dict)`` 取值，而
    ``analyze_and_route_query`` 返回的是 ``{"router": Router(...)}``——pydantic 模型
    不是 dict，判断恒为 False，这一帧**从来没发出去过**。既有夹具
    ``_success_events`` 恰好把 router 写成了 dict，于是"有路由帧"在测试里一直成立、
    在生产里一直不存在——形状失真的夹具比没有夹具更糟。
    """

    @staticmethod
    def _events(router_payload: Any) -> List[Dict[str, Any]]:
        return [
            _ev(
                "on_chain_end",
                name="analyze_and_route_query",
                data={"output": {"router": router_payload}},
                metadata={"langgraph_node": "analyze_and_route_query"},
            ),
            _ev(
                "on_chat_model_stream",
                name="ChatModel",
                data={"chunk": _FakeChunk("这是用来验证路由帧是否上线的流式回答")},
                metadata={"langgraph_node": "create_kb_query"},
            ),
            {
                "event": "on_chain_end",
                "name": "LangGraph",
                "data": {"output": {"sources": []}},
                "metadata": {},
            },
        ]

    @pytest.mark.asyncio
    async def test_router_model_instance_reaches_the_wire(self):
        from agentix.application.agents.router import Router

        frames = await _collect_chat(
            self._events(Router(type="kb-query", logic="fallback: llm down", question="q"))
        )
        routed = [f for f in frames if f.get("route")]
        assert routed, f"Router 实例没换来路由帧：{[f.get('type') for f in frames]}"
        assert routed[0]["type"] == "metadata", f"route 挂在了非 metadata 帧上：{routed[0]}"
        assert routed[0]["route"] == "kb-query"
        assert routed[0]["metadata"]["logic"] == "fallback: llm down"

    @pytest.mark.asyncio
    async def test_dict_shaped_router_payload_also_reaches_the_wire(self):
        """dict 形状（既有夹具/其它节点可能给的形状）同样必须可用。"""
        frames = await _collect_chat(self._events({"type": "general-query", "logic": "fast-path"}))
        routed = [f for f in frames if f.get("route")]
        assert routed and routed[0]["route"] == "general-query"

    @pytest.mark.asyncio
    async def test_absent_router_payload_does_not_fabricate_a_route(self):
        """拿不到路由就整帧不发——不得凭空造一个 route 值（守卫的反向半边）。"""
        frames = await _collect_chat(self._events(None))
        assert not [f for f in frames if f.get("route")], (
            f"没有 router 输出却出现了 route：{[f for f in frames if f.get('route')]}"
        )


# ---------------------------------------------------------------------------
# R5d：流式轨迹通电（error / completion 步）
# ---------------------------------------------------------------------------


class TestStreamTrajectoryRecording:
    @pytest.mark.asyncio
    async def test_stream_exception_records_error_step(self):
        # 只回放首个事件后抛异常：失败发生在 done 之前（完成度校验不触发）
        frames = await _collect_chat(_success_events()[:1], exc=RuntimeError("boom-stream"))

        assert any(f["type"] == "error" for f in frames)
        steps = _all_trajectory_steps()
        error_steps = [s for s in steps if s.kind == "error"]
        assert error_steps, "流式异常必须补记 error 轨迹步（R5d）"
        assert error_steps[0].name == "internal_error"
        assert error_steps[0].status == "error"
        assert not [s for s in steps if s.name == "completion_validator"], (
            "失败路径不得记 completion 步（与 ask 同口径）"
        )

    @pytest.mark.asyncio
    async def test_stream_idle_timeout_records_error_step(self, monkeypatch):
        from agentix.config import settings as app_settings

        monkeypatch.setattr(app_settings, "STREAM_IDLE_TIMEOUT", 0.05)
        graph = _StubStreamGraph(hang=True)  # 永不产出 → idle 超时

        async def _run() -> List[Dict[str, Any]]:
            service = ChatService(graph_getter=lambda: graph)
            return [ev async for ev in service.stream_events("问题", "s-p65-idle")]

        frames = await _run()

        assert any(f["type"] == "error" for f in frames)
        error_steps = [s for s in _all_trajectory_steps() if s.kind == "error"]
        assert error_steps and error_steps[0].name == "stream_timeout", (
            "idle 超时必须补记 stream_timeout 错误步（R5d）"
        )

    @pytest.mark.asyncio
    async def test_stream_success_records_tool_and_completion_steps(self):
        frames = await _collect_chat()

        assert any(f["type"] == "done" for f in frames)
        steps = _all_trajectory_steps()

        tool_steps = [s for s in steps if s.kind == "tool"]
        assert [s.status for s in tool_steps] == ["start", "end"], (
            "工具步照记（既有行为保持）"
        )

        completion_steps = [s for s in steps if s.name == "completion_validator"]
        assert completion_steps, "流式成功终态必须补记 completion 步（R5d）"
        assert completion_steps[0].kind == "node"
        assert completion_steps[0].status == "ok", (
            f"≥6 字答案 + sources 齐全应判 complete: {completion_steps[0].detail}"
        )
        assert not [s for s in steps if s.kind == "error"], "成功路径不得记 error 步"


# ---------------------------------------------------------------------------
# R5d：持久化评估门（jsonl 落盘 → eval 轨迹包导出）
# ---------------------------------------------------------------------------


class TestTrajectoryPersistenceGate:
    @pytest.mark.asyncio
    async def test_stream_failure_jsonl_persists_and_exports_package(
        self, tmp_path, monkeypatch
    ):
        """流式失败轨迹 → JsonlTrajectorySink 落盘 → 导出 eval 轨迹包。

        这是「统一链 SSE 失败可被 trajectory_regression 评估」的最小门：
        首错误步（first_error_step_index）必须穿过持久化层存活。
        """
        from agentix.application.services.trajectory import (
            JsonlTrajectorySink,
            export_trajectory_packages,
            get_trajectory_recorder,
        )
        from agentix.config import settings as app_settings

        sink_path = tmp_path / "traj.jsonl"
        monkeypatch.setattr(app_settings, "TRAJECTORY_SINK", "jsonl", raising=False)
        monkeypatch.setattr(app_settings, "TRAJECTORY_PATH", str(sink_path), raising=False)
        reset_trajectory_recorder()  # 让记录器以 jsonl sink 重建

        frames = await _collect_chat(
            _success_events()[:1],  # 只回放首个事件后抛异常 → 失败流
            exc=RuntimeError("gate-boom"),
            session_id="s-p65-gate",
        )
        assert any(f["type"] == "error" for f in frames)

        rec = get_trajectory_recorder()
        assert isinstance(rec._sink, JsonlTrajectorySink), "必须以 jsonl sink 持久化"
        assert sink_path.exists(), "流式失败轨迹必须落盘"

        out_dir = tmp_path / "packages"
        count = export_trajectory_packages(rec, out_dir)
        assert count >= 1, "eval 必须能导出轨迹包"

        packages = list(out_dir.glob("*.json"))
        assert packages
        package = json.loads(packages[0].read_text(encoding="utf-8"))
        assert package["step_count"] >= 1
        assert package["first_error_step_index"] is not None, (
            "首错误步定位必须穿过持久化层（评估门核心断言）"
        )
        assert package["first_error"]["kind"] == "error"
