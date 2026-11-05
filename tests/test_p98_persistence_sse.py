"""持久化 + SSE 后端验收（p98）。

覆盖规划 §4 W4 四项：
1. 事件批写：flag-off 逐条直写（pipeline 未用 = 基线）、flag-on 满批/定时
   flush 经单次 pipeline 提交、批写批读一致（flush 后 wait/async_get/
   list_recent 全部可见且内容/顺序恒等）、去重语义保真、flush 失败回滚
   dedup 并上抛、aclose 关停兜底、默认 0/0=关；
2. 轨迹异步写：flag-off 同步直写、flag-on 只入队（读回为空）→ flush 兜底
   落 sink 且 step_index 顺序恒等、writer 自然消费、无 loop 降级同步、
   jsonl 兜底落盘（新实例读回）、reset 取消 writer；
3. SSE 合并：flag-off 返回原 source（逐帧恒等）、token 帧内容恒等合并、
   终态/非 token 帧前必 flush（顺序恒等）、finished 帧不合并且前 flush、
   三类 token 类型、源异常传播；
4. SSE 背压：flag-off 直通、满则丢非关键帧（sse_dropped_frames_total 计数）
   保终态帧、源异常传播、消费者提前关闭 → producer 取消且源 generator
   收尾；组合门控内容恒等终态最后。

运行：./.venv/Scripts/python.exe -m pytest tests/test_p98_persistence_sse.py -q
"""
import asyncio
from typing import Any, Dict, List, Optional, Tuple

import pytest

from agentix.config import settings

# ===========================================================================
# 隔离夹具
# ===========================================================================


@pytest.fixture(autouse=True)
def _w4_isolate(monkeypatch):
    from agentix.application.services.trajectory import reset_trajectory_recorder
    from agentix.agent.events.port import reset_event_port

    reset_trajectory_recorder()
    reset_event_port()
    yield
    reset_trajectory_recorder()
    reset_event_port()


def _sample(name: str, **labels) -> float:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name, labels) or 0.0


# ===========================================================================
# 1. 事件批写 —— 内联 fake redis（支持 pipeline 语义）
# ===========================================================================


class _FakePipe:
    def __init__(self, owner: "_FakeRedis", transaction: bool = True) -> None:
        self._owner = owner
        self._ops: List[Tuple[str, tuple]] = []

    def xadd(self, key: str, fields: Dict[str, Any], **kw):
        # T10 起 XADD 带 maxlen/approximate（近似裁剪）；本桩只记传参，不真裁剪。
        self._ops.append(("xadd", (key, fields, kw)))
        return self

    def set(self, key: str, value: str, **kw):
        self._ops.append(("set", (key, value, kw)))
        return self

    def rpush(self, key: str, value: str):
        self._ops.append(("rpush", (key, value)))
        return self

    def ltrim(self, key: str, start: int, end: int):
        self._ops.append(("ltrim", (key, start, end)))
        return self

    async def execute(self):
        if self._owner.fail_execute:
            raise RuntimeError("connection down")
        self._owner.pipeline_batches.append(list(self._ops))
        out = []
        for name, args in self._ops:
            if name == "xadd":
                out.append(self._owner._apply_xadd(*args[:2]))
            elif name == "set":
                out.append(self._owner._apply_set(*args))
            elif name == "rpush":
                out.append(self._owner._apply_rpush(*args))
            elif name == "ltrim":
                out.append(self._owner._apply_ltrim(*args))
        self._owner.direct_calls.append(("pipeline.execute", len(self._ops)))
        return out


class _FakeRedis:
    """覆盖 RedisStreamEventBus 用到的命令面（set/get/delete/xadd/xrange/
    xdel/rpush/ltrim/lrange/pipeline/scan_iter）。"""

    def __init__(self) -> None:
        self.store: Dict[str, str] = {}
        self.streams: Dict[str, List[Tuple[str, Dict[str, str]]]] = {}
        self.lists: Dict[str, List[str]] = {}
        self.index: Dict[str, int] = {}
        self.direct_calls: List[Tuple[str, tuple]] = []
        self.pipeline_batches: List[list] = []
        self.fail_execute = False

    # -- 直调（基线逐条路径） ------------------------------------------------
    async def set(self, key: str, value: str, nx: bool = False, **kw):
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.direct_calls.append(("set", (key,)))
        return True

    async def get(self, key: str) -> Optional[str]:
        return self.store.get(key)

    async def delete(self, *keys: str) -> int:
        n = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                n += 1
        self.direct_calls.append(("delete", keys))
        return n

    async def xadd(self, key: str, fields: Dict[str, str], **kw):
        # T10 起 XADD 带 maxlen/approximate；本桩只记传参，不真裁剪。
        eid = self._apply_xadd(key, fields)
        self.direct_calls.append(("xadd", (key,)))
        return eid

    async def xrange(self, key: str, count: int = 100):
        return list(self.streams.get(key, []))[:count]

    async def xdel(self, key: str, eid: str) -> int:
        entries = self.streams.get(key, [])
        for i, (existing, _f) in enumerate(entries):
            if existing == eid:
                del entries[i]
                return 1
        return 0

    async def rpush(self, key: str, value: str):
        self._apply_rpush(key, value)

    async def ltrim(self, key: str, start: int, end: int):
        self._apply_ltrim(key, start, end)

    async def lrange(self, key: str, start: int, end: int):
        lst = self.lists.get(key, [])
        if end == -1:
            return list(lst[start:])
        return list(lst[start : end + 1])

    def scan_iter(self, match: str = ""):
        prefix = match[:-1] if match.endswith("*") else match

        async def _gen():
            for k in list(self.streams):
                if k.startswith(prefix):
                    yield k

        return _gen()

    def pipeline(self, transaction: bool = True) -> _FakePipe:
        return _FakePipe(self, transaction)

    # -- pipe 应用 -----------------------------------------------------------
    def _apply_xadd(self, key: str, fields: Dict[str, str]) -> str:
        self.index[key] = self.index.get(key, 0) + 1
        eid = f"{self.index[key]}-0"
        self.streams.setdefault(key, []).append((eid, dict(fields)))
        return eid

    def _apply_set(self, key: str, value: str, kw: dict) -> bool:
        if kw.get("nx") and key in self.store:
            return None
        self.store[key] = value
        return True

    def _apply_rpush(self, key: str, value: str) -> int:
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    def _apply_ltrim(self, key: str, start: int, end: int) -> bool:
        lst = self.lists.get(key, [])
        self.lists[key] = lst[start : end + 1]
        return True


def _make_batch_bus(monkeypatch, *, max_size: int, flush_ms: int) -> Any:
    from agentix.agent.events.redis_stream import RedisStreamEventBus

    monkeypatch.setattr(settings, "EVENT_BATCH_MAX_SIZE", max_size)
    monkeypatch.setattr(settings, "EVENT_BATCH_FLUSH_MS", flush_ms)
    fake = _FakeRedis()
    return RedisStreamEventBus(prefix="agentix:test:", client=fake), fake


class TestEventBatching:
    @pytest.mark.asyncio
    async def test_defaults_off_keeps_direct_write(self):
        """默认 0/0 = 关：逐条直写，pipeline 不参与（规划按实修订）。"""
        from agentix.agent.events.redis_stream import RedisStreamEventBus

        assert settings.EVENT_BATCH_MAX_SIZE == 0
        assert settings.EVENT_BATCH_FLUSH_MS == 0
        fake = _FakeRedis()
        bus = RedisStreamEventBus(prefix="agentix:test:", client=fake)
        eid = await bus.publish("t", {"n": 1})
        assert any(c == "xadd" for c, _a in fake.direct_calls)  # 直调 xadd
        assert fake.pipeline_batches == []  # 未走 pipeline
        got = await bus.wait("t", timeout_s=0)
        assert got is not None and got["event_id"] == eid  # 即时可见（基线）

    @pytest.mark.asyncio
    async def test_flag_on_pending_until_flush_then_consistent(
        self, monkeypatch
    ):
        """批写：publish 入队不落库；flush 后批读一致（wait/get/list 顺序）。"""
        bus, fake = _make_batch_bus(monkeypatch, max_size=50, flush_ms=60_000)
        ids = [await bus.publish("t", {"n": i}) for i in range(3)]

        # flush 前：写面为空（可见性延迟 = 批写固有语义），但 dedup 已即时
        assert fake.streams == {}
        assert fake.pipeline_batches == []
        # 手动 flush（0/0 关定时器 ⇒ 测试显式提交）
        assert await bus.flush() == 3
        assert len(fake.pipeline_batches) == 1  # 单次 pipeline 提交
        batch = fake.pipeline_batches[0]
        xadd_ops = [op for op in batch if op[0] == "xadd"]
        set_ops = [op for op in batch if op[0] == "set"]
        rpush_ops = [op for op in batch if op[0] == "rpush"]
        assert len(xadd_ops) == len(set_ops) == len(rpush_ops) == 3
        assert any(op[0] == "ltrim" for op in batch)

        # 批读一致：顺序 + 内容 + rec 可见
        got = []
        while True:
            item = await bus.wait("t", timeout_s=0)
            if item is None:
                break
            got.append(item)
        assert [g["event_id"] for g in got] == ids
        assert [g["payload"]["n"] for g in got] == [0, 1, 2]
        rec = await bus.async_get(ids[1])
        assert rec is not None and rec.payload == {"n": 1}
        recent = await bus.list_recent("t")
        assert [r["event_id"] for r in recent] == list(reversed(ids))  # 新→旧

    @pytest.mark.asyncio
    async def test_full_batch_flushes_eagerly(self, monkeypatch):
        bus, fake = _make_batch_bus(monkeypatch, max_size=3, flush_ms=60_000)
        await bus.publish("t", {"n": 1})
        await bus.publish("t", {"n": 2})
        assert fake.pipeline_batches == []  # 未满不写
        await bus.publish("t", {"n": 3})  # 满批 → 立即 flush（不等定时器）
        assert len(fake.pipeline_batches) == 1

    @pytest.mark.asyncio
    async def test_timer_flush_lands(self, monkeypatch):
        bus, fake = _make_batch_bus(monkeypatch, max_size=50, flush_ms=5)
        await bus.publish("t", {"n": 1})
        assert fake.streams == {}
        await asyncio.sleep(0.05)
        assert len(fake.pipeline_batches) == 1  # 定时 flush 兜底
        assert fake.streams.get("agentix:test:stream:t")

    @pytest.mark.asyncio
    async def test_dedup_semantics_preserved_under_batching(self, monkeypatch):
        bus, _fake = _make_batch_bus(monkeypatch, max_size=50, flush_ms=60_000)
        a = await bus.publish("t", {"n": 1}, event_key="K")
        b = await bus.publish("t", {"n": 2}, event_key="K")
        assert b == a  # 去重返回首次 id（dedup 即时，批写不改语义）
        assert await bus.flush() == 1  # 只有赢家入批

    @pytest.mark.asyncio
    async def test_flush_failure_rolls_back_dedup_and_raises(self, monkeypatch):
        bus, fake = _make_batch_bus(monkeypatch, max_size=50, flush_ms=60_000)
        await bus.publish("t", {"n": 1}, event_key="K")
        dedup_key = next(k for k in fake.store if "dedup" in k)
        fake.fail_execute = True
        with pytest.raises(RuntimeError, match="connection down"):
            await bus.flush()
        assert dedup_key not in fake.store  # 全批 dedup 回滚
        # 回滚后同 key 可重发（非永久占位）
        fake.fail_execute = False
        await bus.publish("t", {"n": 9}, event_key="K")
        assert await bus.flush() == 1

    @pytest.mark.asyncio
    async def test_aclose_flushes_tail(self, monkeypatch):
        bus, fake = _make_batch_bus(monkeypatch, max_size=50, flush_ms=60_000)
        await bus.publish("t", {"n": 1})
        await bus.aclose()  # 关停兜底：取消定时器 + 提交尾巴
        assert fake.streams.get("agentix:test:stream:t")
        assert fake.direct_calls or fake.pipeline_batches

    @pytest.mark.asyncio
    async def test_flag_off_ignores_batch_helpers(self, monkeypatch):
        bus, fake = _make_batch_bus(monkeypatch, max_size=50, flush_ms=60_000)
        monkeypatch.setattr(settings, "EVENT_BATCH_MAX_SIZE", 0)  # 显式关
        await bus.publish("t", {"n": 1})
        assert fake.pipeline_batches == []
        assert await bus.flush() == 0  # 无 pending = no-op


# ===========================================================================
# 2. 轨迹异步写
# ===========================================================================


class TestTrajectoryAsyncWrite:
    @pytest.mark.asyncio
    async def test_flag_off_writes_synchronously(self, monkeypatch):
        from agentix.application.services.trajectory import (
            get_trajectory_recorder,
        )

        monkeypatch.setattr(settings, "TRAJECTORY_ASYNC_WRITE_ENABLED", False)
        assert settings.TRAJECTORY_ASYNC_WRITE_ENABLED is False  # 默认关
        recorder = get_trajectory_recorder()
        recorder.record(kind="tool", name="a", trace_id="t1")
        assert len(recorder.get("t1")) == 1  # 同步立即可读（基线）

    @pytest.mark.asyncio
    async def test_flag_on_queues_then_flush_lands_in_order(self, monkeypatch):
        from agentix.application.services.trajectory import (
            flush_trajectory_writes,
            get_trajectory_recorder,
        )

        monkeypatch.setattr(settings, "TRAJECTORY_ASYNC_WRITE_ENABLED", True)
        recorder = get_trajectory_recorder()
        for i in range(5):
            recorder.record(kind="tool", name=f"s{i}", trace_id="t1")
        assert recorder.get("t1") == []  # 入队未落（读回为空）

        flushed = await flush_trajectory_writes()
        assert flushed == 5
        steps = recorder.get("t1")
        assert [s.step_index for s in steps] == [0, 1, 2, 3, 4]  # 顺序恒等
        assert [s.name for s in steps] == [f"s{i}" for i in range(5)]

    @pytest.mark.asyncio
    async def test_writer_consumes_without_explicit_flush(self, monkeypatch):
        from agentix.application.services.trajectory import (
            get_trajectory_recorder,
        )

        monkeypatch.setattr(settings, "TRAJECTORY_ASYNC_WRITE_ENABLED", True)
        recorder = get_trajectory_recorder()
        recorder.record(kind="llm", name="a", trace_id="t1")
        await asyncio.sleep(0.02)  # 后台 writer 自行消费
        assert len(recorder.get("t1")) == 1

    @pytest.mark.asyncio
    async def test_noop_flush_when_no_recorder_or_empty(self, monkeypatch):
        from agentix.application.services.trajectory import flush_trajectory_writes

        assert await flush_trajectory_writes() == 0  # 无单例 = no-op

        from agentix.application.services.trajectory import (
            get_trajectory_recorder,
        )

        monkeypatch.setattr(settings, "TRAJECTORY_ASYNC_WRITE_ENABLED", True)
        get_trajectory_recorder()  # 建单例但零记录
        assert await flush_trajectory_writes() == 0

    @pytest.mark.asyncio
    async def test_jsonl_sink_flush_lands_on_disk(self, monkeypatch, tmp_path):
        from agentix.application.services.trajectory import (
            flush_trajectory_writes,
            get_trajectory_recorder,
        )

        target = tmp_path / "traj.jsonl"
        monkeypatch.setattr(settings, "TRAJECTORY_SINK", "jsonl")
        monkeypatch.setattr(settings, "TRAJECTORY_PATH", str(target))
        monkeypatch.setattr(settings, "TRAJECTORY_ASYNC_WRITE_ENABLED", True)
        recorder = get_trajectory_recorder()  # 按 settings 构建 jsonl sink
        recorder.record(kind="tool", name="a", trace_id="t1")
        recorder.record(kind="tool", name="b", trace_id="t1")
        await flush_trajectory_writes()
        assert target.exists()  # 兜底落盘
        lines = target.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        # 新实例（模拟重启）读回内容一致
        from agentix.application.services.trajectory import (
            JsonlTrajectorySink,
        )

        fresh = JsonlTrajectorySink(target)
        names = [s.name for s in fresh.list("t1")]
        assert names == ["a", "b"]

    def test_sync_context_falls_back_sync_write_standalone(self, monkeypatch):
        from agentix.application.services.trajectory import TrajectoryRecorder

        monkeypatch.setattr(settings, "TRAJECTORY_ASYNC_WRITE_ENABLED", True)
        recorder = TrajectoryRecorder()
        assert recorder._async_enabled() is False  # 同步测试 = 无 loop
        recorder.record(kind="tool", name="solo", trace_id="ts")
        assert len(recorder.get("ts")) == 1  # 降级同步直写

    @pytest.mark.asyncio
    async def test_reset_cancels_writer_and_clears(self, monkeypatch):
        from agentix.application.services.trajectory import (
            get_trajectory_recorder,
            reset_trajectory_recorder,
        )

        monkeypatch.setattr(settings, "TRAJECTORY_ASYNC_WRITE_ENABLED", True)
        recorder = get_trajectory_recorder()
        recorder.record(kind="tool", name="a", trace_id="t1")
        reset_trajectory_recorder()
        assert recorder.get("t1") == []  # 隔离清空
        await asyncio.sleep(0.01)  # 被取消的 writer 不再写
        assert recorder.get("t1") == []


# ===========================================================================
# 3. SSE 合并
# ===========================================================================


def _tok(content: str) -> Dict[str, Any]:
    return {"type": "message", "content": content}


async def _collect(source) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    async for ev in source:
        out.append(ev)
    return out


class TestSseCoalesce:
    @pytest.mark.asyncio
    async def test_defaults_off_and_passthrough(self):
        from agentix.application.services.stream_gate import gate_events

        assert settings.SSE_COALESCE_ENABLED is False
        assert settings.SSE_QUEUE_SIZE == 0

        async def src():
            for i in range(3):
                yield _tok(str(i))

        original = src()
        try:
            assert gate_events(original) is original  # 双关 = 原 source（零开销）
        finally:
            await original.aclose()

    @pytest.mark.asyncio
    async def test_merged_content_is_concatenation_with_terminal_last(
        self, monkeypatch
    ):
        from agentix.application.services.stream_gate import gate_events

        monkeypatch.setattr(settings, "SSE_COALESCE_ENABLED", True)
        parts = ["你", "好", "世", "界", "，", "答", "案"]

        async def src():
            for p in parts:
                yield _tok(p)
                await asyncio.sleep(0.004)
            yield {"type": "done", "metadata": {"sources": []}}

        frames = await _collect(gate_events(src(), window_s=0.02))
        # 内容恒等：所有 message 帧 content 拼接 == 原文
        merged_text = "".join(
            str(f.get("content") or "") for f in frames if f.get("type") == "message"
        )
        assert merged_text == "".join(parts)
        # 渲染次数显著减少（7 token → 若干合并帧），终态最后且只出现一次
        token_frames = [f for f in frames if f.get("type") == "message"]
        assert 1 <= len(token_frames) < len(parts)
        assert frames[-1]["type"] == "done"
        assert sum(1 for f in frames if f["type"] == "done") == 1

    @pytest.mark.asyncio
    async def test_terminal_and_non_token_frames_flush_in_order(self):
        """假时钟确定性：终态/非 token 帧到达时先 flush 缓冲再放行。"""
        from agentix.application.services.stream_gate import _coalesce_source

        clock = {"t": 0.0}
        emitted: List[Dict[str, Any]] = []

        async def src():
            yield _tok("a")
            clock["t"] += 0.01  # 窗口内
            yield _tok("b")
            clock["t"] += 0.01
            yield {"type": "tool_start", "metadata": {"tool": "search"}}
            clock["t"] += 0.10  # 过期（但 tool_start 非 token，直接 flush+放行）
            yield _tok("c")
            clock["t"] += 0.10  # 过期
            yield _tok("d")

        gen = _coalesce_source(src(), window_s=0.05, time_fn=lambda: clock["t"])
        async for ev in gen:
            emitted.append(ev)

        # a+b 窗内合并；tool_start 触发 flush 前置；c 过期后开窗、d 到达时
        # flush c、流结束 flush d —— 顺序恒等
        assert [e["type"] for e in emitted] == [
            "message",
            "tool_start",
            "message",
            "message",
        ]
        assert emitted[0]["content"] == "ab"
        assert emitted[2]["content"] == "c"
        assert emitted[3]["content"] == "d"
        assert (
            "".join(
                str(e.get("content") or "")
                for e in emitted
                if e.get("type") == "message"
            )
            == "abcd"
        )

    @pytest.mark.asyncio
    async def test_finished_chunk_not_merged_and_flushes_before(self):
        from agentix.application.services.stream_gate import _coalesce_source

        clock = {"t": 0.0}
        emitted: List[Dict[str, Any]] = []

        async def src():
            yield {"type": "answer_chunk", "content": "前半"}
            clock["t"] += 0.01
            yield {"type": "answer_chunk", "content": "中间"}
            clock["t"] += 0.01
            yield {
                "type": "answer_chunk",
                "content": "终稿",
                "finished": True,
                "citations": [{"chunk_id": "c1"}],
            }

        gen = _coalesce_source(src(), window_s=0.05, time_fn=lambda: clock["t"])
        async for ev in gen:
            emitted.append(ev)

        assert len(emitted) == 2  # 合并帧 + finished 原帧（不合并且前 flush）
        assert emitted[0]["content"] == "前半中间"
        assert emitted[1]["finished"] is True
        assert emitted[1]["content"] == "终稿"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ftype", ["message", "answer_chunk", "thinking_chunk"])
    async def test_all_token_types_coalesce(self, ftype: str, monkeypatch):
        from agentix.application.services.stream_gate import gate_events

        monkeypatch.setattr(settings, "SSE_COALESCE_ENABLED", True)

        async def src():
            for i in range(6):
                yield {"type": ftype, "content": str(i)}
                await asyncio.sleep(0.004)
            yield {"type": "done"}

        frames = await _collect(gate_events(src(), window_s=0.02))
        text = "".join(
            str(f.get("content") or "") for f in frames if f.get("type") == ftype
        )
        assert text == "012345"  # 内容恒等
        assert len([f for f in frames if f.get("type") == ftype]) < 6

    @pytest.mark.asyncio
    async def test_source_exception_propagates(self, monkeypatch):
        from agentix.application.services.stream_gate import gate_events

        monkeypatch.setattr(settings, "SSE_COALESCE_ENABLED", True)

        async def src():
            yield _tok("x")
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await _collect(gate_events(src(), window_s=0.05))


# ===========================================================================
# 4. SSE 背压
# ===========================================================================


class TestSseBackpressure:
    @pytest.mark.asyncio
    async def test_drops_non_critical_keeps_terminal(self, monkeypatch):
        from agentix.application.services.stream_gate import gate_events

        monkeypatch.setattr(settings, "SSE_QUEUE_SIZE", 1)
        dropped_before = _sample("sse_dropped_frames_total", reason="backpressure")

        async def src():
            yield _tok("t1")
            yield _tok("t2")
            yield _tok("t3")
            yield {"type": "error", "content": "failed"}

        frames = await _collect(gate_events(src()))
        # 终态帧必达（最后），中间 token 被背压丢弃（有界=1 ⇒ 最多持 1）
        assert frames[-1]["type"] == "error"
        received_tokens = [f for f in frames if f["type"] == "message"]
        assert len(received_tokens) < 3
        assert (
            _sample("sse_dropped_frames_total", reason="backpressure")
            - dropped_before
            >= 1
        )

    @pytest.mark.asyncio
    async def test_critical_frames_block_until_delivered(self, monkeypatch):
        """maxsize=1：终态帧阻塞等位，不因队满被丢。"""
        from agentix.application.services.stream_gate import gate_events

        monkeypatch.setattr(settings, "SSE_QUEUE_SIZE", 1)

        async def src():
            yield {"type": "meta", "conversation_id": "c1"}  # critical
            yield _tok("dropped-candidate")
            yield {"type": "done"}  # critical：阻塞等位

        frames = await _collect(gate_events(src()))
        types = [f["type"] for f in frames]
        assert "meta" in types and "done" in types  # 关键帧全部送达
        assert types.index("done") == len(types) - 1

    @pytest.mark.asyncio
    async def test_source_exception_propagates_through_queue(self, monkeypatch):
        from agentix.application.services.stream_gate import gate_events

        monkeypatch.setattr(settings, "SSE_QUEUE_SIZE", 2)

        async def src():
            yield _tok("a")
            raise KeyError("src-fail")

        with pytest.raises(KeyError, match="src-fail"):
            await _collect(gate_events(src()))

    @pytest.mark.asyncio
    async def test_consumer_close_cancels_producer_and_closes_source(
        self, monkeypatch
    ):
        from agentix.application.services.stream_gate import gate_events

        monkeypatch.setattr(settings, "SSE_QUEUE_SIZE", 4)
        closed = {"v": False}

        async def src():
            try:
                for i in range(100):
                    yield _tok(str(i))
                    await asyncio.sleep(0.001)
            finally:
                closed["v"] = True

        gate = gate_events(src())
        got = []
        try:
            async for ev in gate:
                got.append(ev)
                if len(got) >= 1:
                    break
        finally:
            await gate.aclose()  # 消费者提前关闭
        await asyncio.sleep(0.01)
        assert closed["v"] is True  # 源 generator 收尾（producer 被取消）


# ===========================================================================
# 5. 组合门控
# ===========================================================================


class TestCombinedGate:
    @pytest.mark.asyncio
    async def test_coalesce_plus_backpressure_content_identity(
        self, monkeypatch
    ):
        from agentix.application.services.stream_gate import gate_events

        monkeypatch.setattr(settings, "SSE_COALESCE_ENABLED", True)
        monkeypatch.setattr(settings, "SSE_QUEUE_SIZE", 8)
        parts = [f"p{i}" for i in range(10)]

        async def src():
            for p in parts:
                yield _tok(p)
                await asyncio.sleep(0.003)
            yield {"type": "done"}

        frames = await _collect(gate_events(src(), window_s=0.02))
        text = "".join(
            str(f.get("content") or "") for f in frames if f.get("type") == "message"
        )
        # 消费者不慢 ⇒ 队列 8 足够，全程不丢：内容恒等
        assert text == "".join(parts)
        assert frames[-1]["type"] == "done"
        # 合并确实减少了帧数（7 → <10）
        assert len([f for f in frames if f["type"] == "message"]) < len(parts)


# ===========================================================================
# 6. 五处同步守卫（p41 主检 配置管理.md，这里补 selfcheck/.env）
# ===========================================================================


class TestW4FlagSync:
    W4_KEYS = (
        "EVENT_BATCH_MAX_SIZE",
        "EVENT_BATCH_FLUSH_MS",
        "TRAJECTORY_ASYNC_WRITE_ENABLED",
        "SSE_COALESCE_ENABLED",
        "SSE_QUEUE_SIZE",
    )

    def test_defaults_are_off(self):
        assert settings.EVENT_BATCH_MAX_SIZE == 0
        assert settings.EVENT_BATCH_FLUSH_MS == 0
        assert settings.TRAJECTORY_ASYNC_WRITE_ENABLED is False
        assert settings.SSE_COALESCE_ENABLED is False
        assert settings.SSE_QUEUE_SIZE == 0

    def test_selfcheck_allows_omission(self):
        from agentix.config.selfcheck import DEFAULT_IGNORED_MISSING

        for key in self.W4_KEYS:
            assert key in DEFAULT_IGNORED_MISSING, f"{key} 不在 selfcheck 白名单"

    def test_env_example_documents_all_keys(self):
        from pathlib import Path

        env_text = Path(".env.example").read_text(encoding="utf-8")
        for key in self.W4_KEYS:
            assert key in env_text, f"{key} 不在 .env.example"
