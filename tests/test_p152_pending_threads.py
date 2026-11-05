"""LG-2-1：待恢复线程改由 `checkpointer.alist()` 派生（不再读 Redis 集合）。

验收：

① 走一轮未完成请求后该线程能被列出；
② 完成后不再列出；
③ `alist` 抛异常时**降级并 logger.warning**，不许静默返空；
④ 「1 个未完成 + 1 个已完成」场景（本文件第 1 条用例同批断言）；
⑤ 扫描有界：`alist` 必须带 `limit`，翻页有上限（不做无界全表扫，见方案 §7 风险表）；
⑥ `active_threads` 集合与 `sadd` / `srem` / `smembers` 已从模块中删除；
⑦ `min_idle_seconds` 用来挡住「此刻可能仍在跑」的线程（启动恢复的 60 秒闸）。

证据取向：①②④⑦ 用真 `MemorySaver` + 真题跑出 checkpoint（`interrupt_before` 制造
"真的停在 b 之前"的线程），不手搓状态；③⑤⑥ 是形状断言，桩比真 Redis 更能固定输入。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, TypedDict

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from loguru import logger as loguru_logger

from agentix.application.services import breakpoint_resume as br
from agentix.application.services.breakpoint_resume import CheckpointRecovery


class _State(TypedDict, total=False):
    n: int


async def _node_a(state: _State) -> Dict[str, int]:
    return {"n": state.get("n", 0) + 1}


async def _node_b(state: _State) -> Dict[str, int]:
    return {"n": state.get("n", 0) + 10}


class _FakeRedis:
    """只实现元数据读写：`sadd` / `srem` / `smembers` 刻意不实现。

    比 grep 更强的形状守卫——模块若还调用集合命令，用例直接 AttributeError。
    """

    def __init__(self) -> None:
        self.store: Dict[str, str] = {}

    async def get(self, key: str) -> Any:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: Any = None) -> None:
        self.store[key] = value


def _build_graph(saver: MemorySaver):
    builder = StateGraph(_State)
    builder.add_node("a", _node_a)
    builder.add_node("b", _node_b)
    builder.add_edge(START, "a")
    builder.add_edge("a", "b")
    builder.add_edge("b", END)
    return builder.compile(checkpointer=saver)


def _cfg(thread_id: str) -> Dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _capture_warning_logs() -> tuple[List[str], int]:
    captured: List[str] = []
    sink_id = loguru_logger.add(captured.append, level="WARNING")
    return captured, sink_id


@pytest.mark.asyncio
async def test_unfinished_thread_listed_and_dropped_after_completion():
    """验收①②④：一个已跑完 + 一个停在中途 ⇒ 只列后者；续跑完成后不再列出。"""
    saver = MemorySaver()
    app = _build_graph(saver)

    await app.ainvoke({"n": 1}, _cfg("done"))
    # interrupt_before 造出真实的"跑到一半"：最新快照 next == ("b",)
    await app.ainvoke({"n": 2}, _cfg("pending"), interrupt_before=["b"])

    recovery = CheckpointRecovery(redis_client=_FakeRedis(), checkpointer=saver)
    assert await recovery.get_active_threads(app) == ["pending"]

    result = await recovery.resume_thread("pending", app)
    assert result is not None and result["n"] == 13

    assert await recovery.get_active_threads(app) == []


@pytest.mark.asyncio
async def test_resume_thread_does_not_require_redis_metadata():
    """待恢复线程由 checkpoint 派生 ⇒ 没有 Redis 元数据也必须能续跑。"""
    saver = MemorySaver()
    app = _build_graph(saver)
    await app.ainvoke({"n": 5}, _cfg("t-no-meta"), interrupt_before=["b"])

    redis = _FakeRedis()
    recovery = CheckpointRecovery(redis_client=redis, checkpointer=saver)
    assert await recovery.get_checkpoint_metadata("t-no-meta") is None

    result = await recovery.resume_thread("t-no-meta", app)
    assert result is not None and result["n"] == 16


@pytest.mark.asyncio
async def test_unknown_thread_has_no_snapshot_and_no_resume():
    """无 checkpoint 的线程：快照 None（端点据此 404），续跑返回 None（不假成功）。"""
    saver = MemorySaver()
    app = _build_graph(saver)
    recovery = CheckpointRecovery(redis_client=_FakeRedis(), checkpointer=saver)

    assert await recovery.get_thread_snapshot("ghost", app) is None
    assert await recovery.resume_thread("ghost", app) is None


@pytest.mark.asyncio
async def test_alist_failure_warns_and_degrades():
    """验收③：alist 抛异常 ⇒ 返回空列表，但必须留一条 warning（不许静默）。"""

    class _BrokenSaver:
        async def alist(self, config, *, filter=None, before=None, limit=None):
            raise RuntimeError("redis search down")
            yield  # pragma: no cover - async generator 形状

    captured, sink_id = _capture_warning_logs()
    try:
        recovery = CheckpointRecovery(redis_client=_FakeRedis(), checkpointer=_BrokenSaver())
        assert await recovery.get_active_threads(object()) == []
    finally:
        loguru_logger.remove(sink_id)

    assert any("checkpointer.alist failed" in message for message in captured), captured


@pytest.mark.asyncio
async def test_missing_checkpointer_warns_and_returns_empty(monkeypatch):
    """checkpointer 未装配（init_checkpointer 未调用）⇒ 降级为空 + warning。"""
    from agentix.infrastructure.persistence import checkpointer as ckpt_module

    monkeypatch.setattr(ckpt_module, "get_checkpointer", lambda: None)

    captured, sink_id = _capture_warning_logs()
    try:
        recovery = CheckpointRecovery(redis_client=_FakeRedis())
        assert await recovery.get_active_threads(object()) == []
    finally:
        loguru_logger.remove(sink_id)

    assert any("checkpointer unavailable" in message for message in captured), captured


class _PagedSaver:
    """固定数量的 checkpoint，按 `before` 位置续页，并记录每次调用的实参。"""

    def __init__(self, total: int) -> None:
        self.total = total
        self.calls: List[Dict[str, Any]] = []
        self._ids = [f"ckpt-{i:05d}" for i in range(total)]

    async def alist(self, config, *, filter=None, before=None, limit=None):
        self.calls.append({"config": config, "before": before, "limit": limit})
        start = 0
        if before is not None:
            start = int(before["configurable"]["checkpoint_id"].split("-")[1]) + 1
        for index in range(start, min(start + (limit or 10_000), self.total)):
            yield SimpleNamespace(
                config={
                    "configurable": {
                        "thread_id": f"t{index}",
                        "checkpoint_ns": "",
                        "checkpoint_id": self._ids[index],
                    }
                }
            )


class _AllUnfinishedGraph:
    async def aget_state(self, config):
        return SimpleNamespace(next=("b",), interrupts=(), created_at=None, values={"n": 1})


@pytest.mark.asyncio
async def test_scan_is_paged_with_bounded_limit():
    """验收⑤：跨线程列举必须带 limit 并按页续扫（450 条 ⇒ 3 页）。"""
    saver = _PagedSaver(total=450)
    recovery = CheckpointRecovery(redis_client=_FakeRedis(), checkpointer=saver)

    threads = await recovery.get_active_threads(_AllUnfinishedGraph())

    assert len(threads) == 450
    assert [call["limit"] for call in saver.calls] == [br.THREAD_SCAN_PAGE_SIZE] * 3
    assert all(call["config"] is None for call in saver.calls), "跨线程列举必须传 config=None"
    assert saver.calls[0]["before"] is None
    assert saver.calls[1]["before"]["configurable"]["checkpoint_id"] == "ckpt-00199"
    assert saver.calls[2]["before"]["configurable"]["checkpoint_id"] == "ckpt-00399"


@pytest.mark.asyncio
async def test_scan_stops_at_page_cap_instead_of_scanning_everything():
    """验收⑤：翻页有上限，不做无界全表扫。"""
    saver = _PagedSaver(total=br.THREAD_SCAN_PAGE_SIZE * (br.THREAD_SCAN_MAX_PAGES + 2))
    recovery = CheckpointRecovery(redis_client=_FakeRedis(), checkpointer=saver)

    await recovery.get_active_threads(_AllUnfinishedGraph())

    assert len(saver.calls) == br.THREAD_SCAN_MAX_PAGES


@pytest.mark.asyncio
async def test_min_idle_seconds_filters_fresh_threads():
    """验收⑦：启动恢复的 60 秒闸——刚写过 checkpoint 的线程不当作崩溃受害者。"""
    saver = MemorySaver()
    app = _build_graph(saver)
    await app.ainvoke({"n": 1}, _cfg("pending"), interrupt_before=["b"])

    recovery = CheckpointRecovery(redis_client=_FakeRedis(), checkpointer=saver)
    assert await recovery.get_active_threads(app) == ["pending"]
    assert await recovery.get_active_threads(app, min_idle_seconds=60) == []


@pytest.mark.asyncio
async def test_snapshot_read_failure_skips_only_that_thread():
    """单线程读状态失败 ⇒ 跳过它、其余照常列出（辅助诊断不拖垮整轮扫描）。"""

    class _Saver:
        async def alist(self, config, *, filter=None, before=None, limit=None):
            for thread_id in ("bad", "good"):
                yield SimpleNamespace(
                    config={
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": "",
                            "checkpoint_id": f"ckpt-{thread_id}",
                        }
                    }
                )

    class _Graph:
        async def aget_state(self, config):
            if config["configurable"]["thread_id"] == "bad":
                raise RuntimeError("boom")
            return SimpleNamespace(next=("b",), interrupts=(), created_at=None, values={"n": 1})

    captured, sink_id = _capture_warning_logs()
    try:
        recovery = CheckpointRecovery(redis_client=_FakeRedis(), checkpointer=_Saver())
        assert await recovery.get_active_threads(_Graph()) == ["good"]
    finally:
        loguru_logger.remove(sink_id)

    assert any("state snapshot unavailable for thread bad" in message for message in captured)


def test_active_threads_set_and_commands_removed_from_module():
    """验收⑥：`active_threads` 集合与集合命令已从模块中消失。

    按 AST 判而不是按原文判：模块注释里**必须**能提这件事（解释为什么换源头），
    原文判会把解释性注释也判成犯例，逼注释消失。这里只看真实调用与字符串字面量。
    """
    tree = ast.parse(Path(br.__file__).read_text(encoding="utf-8"))

    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"sadd", "srem", "smembers"}
    }
    assert calls == set(), f"LG-2-1 之后模块里不该再调用集合命令：{calls}"

    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    leftovers = [text for text in literals if "active_threads" in text]
    assert not leftovers, f"LG-2-1 之后模块里不该再拼 active_threads 键：{leftovers}"


@pytest.mark.asyncio
async def test_save_checkpoint_metadata_writes_status_without_set_commands():
    """元数据写入保留，集合维护删除：fake redis 未实现 sadd/srem 也照常走完。"""
    redis = _FakeRedis()
    recovery = CheckpointRecovery(redis_client=redis)

    await recovery.save_checkpoint_metadata(
        "t-1", "execute_task", {"k": 1}, status="in_progress"
    )

    raw = redis.store["checkpoint_recovery:t-1"]
    assert json.loads(raw)["status"] == "in_progress"
    assert json.loads(raw)["last_node"] == "execute_task"
