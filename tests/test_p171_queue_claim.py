"""T26：队列 PEL 认领（XAUTOCLAIM）+ 优雅停机。

覆盖面与验收口径（规划文档 §4.2 `#### T26` 卡）：
- **① 死消费者 PEL 被有界认领**：``idle`` 阈值 ``_CLAIM_IDLE_MS`` / 周期
  ``_CLAIM_INTERVAL_S`` / 单轮条数 ``_CLAIM_MAX_PER_ROUND`` 三个模块常量生效；
  认领到的消息走**同一份** ``_process_message``（不复制处理逻辑，纪律 6 ④）；
  单轮不超过上限；
- **② 停机对在飞 run/流显式收口**：``main._start_task_queue_worker`` 起的任务在
  ``_shutdown`` 里被 ``stop_worker()`` + ``cancel()`` 收口、句柄归 ``None``；
- **③ 认领与收口失败可观测**：认领失败 ``logger.warning``（带异常类型）+
  指标 ``task_queue_claimed_total{result="error"}``，且消费循环不被一次认领失败打死；
- **④ compose 不动**：本文件不校验 compose（未改 compose ⇒ 不需要
  ``gen_staging_matrix.py --check``）。

另外钉住三件容易做错的事：
- ``XAUTOCLAIM`` 返回值形状由**服务端 Redis 版本**决定（6.2 两段 / 7.x 三段），
  归一化只在 ``_normalize_xautoclaim_response`` 一处；
- flag 关（默认）⇒ ``_start_task_queue_worker()`` 返回 False、句柄 ``None``、
  **零日志、零 import、零 ``get_task_queue()`` 调用**（纪律 1）；
- 指标 ``result`` 是封闭枚举 ``{"ok","empty","error"}``，且 prometheus 不可用时的
  ``_NoopMetric`` 兜底表里有这一行（漏了会让指标在降级环境静默消失）。

**不连真 Redis**：``redis.asyncio.Redis`` 一律用本文件的 ``_FakeRedis`` 顶替；
**不 sleep 真实长时间**：需要等循环转圈的地方把 ``_CLAIM_INTERVAL_S`` 临时压到毫秒级。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Dict, List, Optional, Tuple

import pytest
from loguru import logger

from agentix.application.services import task_queue as tq
from agentix.application.services.task_queue import (
    TaskQueue,
    _CLAIM_IDLE_MS,
    _CLAIM_INTERVAL_S,
    _CLAIM_MAX_PER_ROUND,
    _normalize_xautoclaim_response,
)
from agentix.config import settings
from agentix.observability import metrics

CLAIM_RESULTS = frozenset({"ok", "empty", "error"})


# ---------------------------------------------------------------------------
# 桩与工具
# ---------------------------------------------------------------------------


class _FakeRedis:
    """只实现 worker 用到的四条命令的假客户端（绝不建真连接）。"""

    def __init__(
        self,
        *,
        claim_response: Any = ("0-0", []),
        claim_error: Optional[BaseException] = None,
        read_response: Any = None,
    ) -> None:
        self._claim_response = claim_response
        self._claim_error = claim_error
        self._read_response = read_response
        self.claim_calls: List[Dict[str, Any]] = []
        self.read_calls = 0
        self.acks: List[Tuple[str, str, str]] = []

    async def xgroup_create(self, *args: Any, **kwargs: Any) -> bool:
        return True

    async def xautoclaim(self, *args: Any, **kwargs: Any) -> Any:
        self.claim_calls.append({"args": args, "kwargs": kwargs})
        if self._claim_error is not None:
            raise self._claim_error
        return self._claim_response

    async def xreadgroup(self, *args: Any, **kwargs: Any) -> Any:
        self.read_calls += 1
        # 让出事件循环（等价于真实客户端在 block 上停住），否则取消点不可达。
        await asyncio.sleep(0.01)
        return self._read_response

    async def xack(self, stream: str, group: str, message_id: str) -> int:
        self.acks.append((stream, group, message_id))
        return 1


def _make_queue(redis: _FakeRedis, *, handlers: Optional[Dict[str, Any]] = None) -> TaskQueue:
    """绕开 ``__init__``（它会 ``Redis.from_url``）拼一台待测队列。"""
    queue = TaskQueue.__new__(TaskQueue)
    queue._redis = redis
    queue.stream_name = "agentix:tasks"
    queue.consumer_group = "workers"
    queue.consumer_name = "w-test"
    queue._handlers = handlers or {}
    queue._running = False
    queue._background_tasks = set()
    return queue


@pytest.fixture()
def warning_logs():
    """捕获 loguru WARNING+ 消息（fail-open 的可观测面）。"""
    records: List[str] = []
    handler_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
    try:
        yield records
    finally:
        logger.remove(handler_id)


@pytest.fixture()
def claim_results(monkeypatch) -> List[str]:
    """拦截指标记录，拿到调用点实际写入的 ``result`` 序列。"""
    recorded: List[str] = []
    monkeypatch.setattr(tq, "record_task_queue_claimed", recorded.append)
    return recorded


def _entries(n: int) -> List[Tuple[str, Dict[str, str]]]:
    return [
        (f"{i + 1}-0", {"task_id": f"t{i}", "task_type": "demo", "payload": "{}"}) for i in range(n)
    ]


# ---------------------------------------------------------------------------
# ① 认领：有界、归一化、走同一份 _process_message
# ---------------------------------------------------------------------------


class TestClaimNormalization:
    def test_two_element_shape(self):
        """Redis 6.2 起：``[next-cursor, entries]``。"""
        next_id, entries = _normalize_xautoclaim_response(("5-0", _entries(2)))
        assert next_id == "5-0"
        assert [mid for mid, _ in entries] == ["1-0", "2-0"]

    def test_three_element_shape(self):
        """Redis 7.x：``[next-cursor, entries, deleted-ids]``——第三段必须被丢掉。"""
        next_id, entries = _normalize_xautoclaim_response(("9-0", _entries(1), ["8-0"]))
        assert next_id == "9-0"
        assert [mid for mid, _ in entries] == ["1-0"]

    def test_none_valued_entries_are_dropped(self):
        """Redis 7 早期把已删除消息放在 entries 里（值为 None）：不能进处理器。"""
        _next_id, entries = _normalize_xautoclaim_response(
            ("0-0", [("1-0", {"task_id": "t"}), ("2-0", None)])
        )
        assert [mid for mid, _ in entries] == ["1-0"]

    def test_empty_and_unexpected_shapes_degrade_to_no_entries(self, warning_logs):
        assert _normalize_xautoclaim_response(None) == ("0-0", [])
        assert _normalize_xautoclaim_response([]) == ("0-0", [])
        # 形状完全意外（代理/桩返回别的东西）⇒ 当作没东西可认领，但要留痕
        assert _normalize_xautoclaim_response({"oops": 1}) == ("0-0", [])
        assert any("Unexpected XAUTOCLAIM response shape" in r for r in warning_logs)


class TestClaimRound:
    @pytest.mark.parametrize("three_elements", [False, True])
    @pytest.mark.asyncio
    async def test_claimed_messages_go_through_the_shared_process_message(
        self, monkeypatch, claim_results, three_elements
    ):
        """两种返回形状都能被归一化，且**同一份** `_process_message` 被调 N 次。"""
        entries = _entries(3)
        raw = ("0-0", entries, []) if three_elements else ("0-0", entries)
        stub = _FakeRedis(claim_response=raw)
        queue = _make_queue(stub)

        processed: List[Tuple[str, Dict[str, str]]] = []

        async def spy(message_id: str, data: Dict[str, str]) -> None:
            processed.append((message_id, data))

        monkeypatch.setattr(queue, "_process_message", spy)

        claimed = await queue._claim_stale_pending()

        assert claimed == 3
        assert [mid for mid, _ in processed] == ["1-0", "2-0", "3-0"]
        assert claim_results == ["ok"]
        # 认领参数取自模块常量，且每次只认一轮
        assert len(stub.claim_calls) == 1
        kwargs = stub.claim_calls[0]["kwargs"]
        assert kwargs["min_idle_time"] == _CLAIM_IDLE_MS
        assert kwargs["count"] == _CLAIM_MAX_PER_ROUND
        assert kwargs["start_id"] == "0-0"

    @pytest.mark.asyncio
    async def test_single_round_is_capped(self, monkeypatch, claim_results):
        """桩返回超过上限的条目时，单轮只处理上限条（有界，不一次拖光 PEL）。"""
        stub = _FakeRedis(claim_response=("0-0", _entries(_CLAIM_MAX_PER_ROUND + 5)))
        queue = _make_queue(stub)

        processed: List[str] = []

        async def spy(message_id: str, data: Dict[str, str]) -> None:
            processed.append(message_id)

        monkeypatch.setattr(queue, "_process_message", spy)

        claimed = await queue._claim_stale_pending()

        assert len(processed) == _CLAIM_MAX_PER_ROUND
        assert claimed == _CLAIM_MAX_PER_ROUND

    @pytest.mark.asyncio
    async def test_no_claimable_messages_counts_empty(self, claim_results):
        stub = _FakeRedis(claim_response=("0-0", []))
        queue = _make_queue(stub)

        assert await queue._claim_stale_pending() == 0
        assert claim_results == ["empty"]

    def test_module_constants_encode_the_bound(self):
        """三个常量就是验收①的「有界」来源：idle 阈值 + 周期 + 单轮上限。"""
        assert _CLAIM_IDLE_MS == 60_000
        assert _CLAIM_INTERVAL_S == 30.0
        assert 1 <= _CLAIM_MAX_PER_ROUND <= 64


# ---------------------------------------------------------------------------
# ③ 认领失败可观测、且不打死消费循环
# ---------------------------------------------------------------------------


class TestClaimFailureIsObservable:
    @pytest.mark.asyncio
    async def test_claim_error_is_logged_and_counted_without_raising(
        self, warning_logs, claim_results
    ):
        stub = _FakeRedis(claim_error=RuntimeError("XAUTOCLAIM unavailable"))
        queue = _make_queue(stub)

        assert await queue._claim_stale_pending() == 0

        assert claim_results == ["error"]
        joined = "\n".join(warning_logs)
        assert "PEL claim failed" in joined
        assert "RuntimeError" in joined, "留痕必须带异常类型（否则只剩一句失败）"
        assert "XAUTOCLAIM unavailable" in joined, "留痕必须带原因"

    @pytest.mark.asyncio
    async def test_consumption_loop_survives_repeated_claim_failures(
        self, monkeypatch, warning_logs, claim_results
    ):
        """一次（乃至连续）认领失败不许让消费循环退出——它还要读新消息。"""
        # 把认领周期压到毫秒级：只为了让循环在测试窗口里多转几圈（不 sleep 30 秒）。
        monkeypatch.setattr(tq, "_CLAIM_INTERVAL_S", 0.01)
        stub = _FakeRedis(claim_error=RuntimeError("boom"))
        queue = _make_queue(stub)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.start_worker(), timeout=0.2)

        assert stub.read_calls >= 2, "认领失败后必须继续读新消息"
        assert len(stub.claim_calls) >= 2, "下一轮必须继续尝试认领"
        assert claim_results.count("error") >= 2


# ---------------------------------------------------------------------------
# 指标口径
# ---------------------------------------------------------------------------


class TestMetricVocabulary:
    def test_result_values_are_closed(self):
        assert metrics.TASK_QUEUE_CLAIM_RESULT_VALUES == CLAIM_RESULTS

    def test_counter_is_registered_with_bounded_label(self):
        assert metrics.PROMETHEUS_AVAILABLE, "本环境启用了 prometheus_client"
        assert metrics.TASK_QUEUE_CLAIMED._labelnames == ("result",)

    def test_noop_fallback_table_has_the_new_metric(self):
        """prometheus 不可用时 metric 由 ``_NoopMetric`` 兜底；漏一行 ⇒ 指标静默消失。"""
        source = inspect.getsource(metrics)
        assert "TASK_QUEUE_CLAIMED = _NoopMetric()" in source


# ---------------------------------------------------------------------------
# ② 起停接线：flag 关零开销 / flag 开真起任务、关停真收口
# ---------------------------------------------------------------------------


class TestStartWiring:
    @pytest.mark.asyncio
    async def test_flag_off_creates_no_task_no_call_no_log(self, monkeypatch):
        from agentix import main

        monkeypatch.setattr(main, "_task_queue_worker_task", None)
        monkeypatch.setattr(main, "_task_queue_worker", None)
        monkeypatch.setattr(settings, "TASK_QUEUE_WORKER_ENABLED", False, raising=False)

        called: List[int] = []
        monkeypatch.setattr(tq, "get_task_queue", lambda: called.append(1))

        records: List[str] = []
        handler_id = logger.add(lambda m: records.append(str(m)), level="DEBUG")
        try:
            assert main._start_task_queue_worker() is False
        finally:
            logger.remove(handler_id)

        assert main._task_queue_worker_task is None
        assert main._task_queue_worker is None
        assert called == [], "flag 关时连 get_task_queue 都不该被调用（不建连）"
        assert records == [], "flag 关时一行日志都不该有（纪律 1）"

    @pytest.mark.asyncio
    async def test_flag_on_starts_task_and_shutdown_cancels_it(self, monkeypatch):
        from agentix import main

        stub = _FakeRedis(claim_response=("0-0", []))
        queue = _make_queue(stub)
        monkeypatch.setattr(settings, "TASK_QUEUE_WORKER_ENABLED", True, raising=False)
        monkeypatch.setattr(tq, "get_task_queue", lambda: queue)
        monkeypatch.setattr(main, "_task_queue_worker_task", None)
        monkeypatch.setattr(main, "_task_queue_worker", None)

        assert main._start_task_queue_worker() is True
        task = main._task_queue_worker_task
        assert task is not None and not task.done()
        assert main._task_queue_worker is queue

        # 让循环至少转一圈：真的在读消息 + 真的做过认领
        for _ in range(20):
            if stub.read_calls:
                break
            await asyncio.sleep(0.01)
        assert stub.read_calls >= 1
        assert len(stub.claim_calls) == 1, "启动即做一次认领（接手上一个进程留下的 PEL）"

        class _NoopService:
            async def cleanup(self) -> None:
                return None

        monkeypatch.setattr(main, "get_lightrag_service", lambda: _NoopService())

        await main._shutdown()

        assert task.done(), "关停必须把 worker 任务收口"
        assert task.cancelled(), "收口要走取消路径（可区分「被取消」与「自然结束」）"
        assert main._task_queue_worker_task is None
        assert main._task_queue_worker is None
        assert queue._running is False

    @pytest.mark.asyncio
    async def test_start_failure_is_fail_open(self, monkeypatch):
        """构造/启动失败只留痕，不阻断启动（纪律 2）；返回值 False 表示没有任务可取消。"""
        from agentix import main

        monkeypatch.setattr(settings, "TASK_QUEUE_WORKER_ENABLED", True, raising=False)
        monkeypatch.setattr(main, "_task_queue_worker_task", None)
        monkeypatch.setattr(main, "_task_queue_worker", None)

        def boom():
            raise RuntimeError("redis client construction failed")

        monkeypatch.setattr(tq, "get_task_queue", boom)

        records: List[str] = []
        handler_id = logger.add(lambda m: records.append(str(m)), level="ERROR")
        try:
            assert main._start_task_queue_worker() is False
        finally:
            logger.remove(handler_id)

        assert main._task_queue_worker_task is None
        assert main._task_queue_worker is None
        assert any("task queue worker start failed" in r for r in records)


class TestSingleSourceOfDecision:
    def test_start_worker_does_not_read_the_flag(self):
        """「要不要跑」只在 `_start_task_queue_worker` 判一次（纪律 6 ④）。"""
        source = inspect.getsource(tq.TaskQueue.start_worker)
        assert "TASK_QUEUE_WORKER_ENABLED" not in source

    def test_claim_does_not_duplicate_message_processing(self):
        """认领必须复用 `_process_message`，不许复制一份处理逻辑。"""
        source = inspect.getsource(tq.TaskQueue._claim_stale_pending)
        assert "self._process_message(" in source
        for duplicated in ("self._redis.xack", "TaskStatus"):
            assert duplicated not in source, f"认领路径不得自带 {duplicated}（复制了处理逻辑）"

    def test_startup_wires_and_shutdown_closes(self):
        from agentix import main

        startup_source = inspect.getsource(main._startup)
        shutdown_source = inspect.getsource(main._shutdown)
        assert "_start_task_queue_worker()" in startup_source
        assert "stop_worker()" in shutdown_source
        assert "_task_queue_worker_task.cancel()" in shutdown_source
