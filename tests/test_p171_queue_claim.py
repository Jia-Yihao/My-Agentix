"""T26：队列 PEL 认领（XAUTOCLAIM）+ 优雅停机。

覆盖面与验收口径（规划文档 §4.2 `#### T26` 卡）：
- **① 死消费者 PEL 被有界认领**：``idle`` 阈值 ``_CLAIM_IDLE_MS`` / 周期
  ``_CLAIM_INTERVAL_S`` / 单轮条数 ``_CLAIM_MAX_PER_ROUND`` 三个模块常量生效；
  认领到的消息走**同一份** ``_process_message``（不复制处理逻辑，约定 6 ④）；
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
  **零日志、零 import、零 ``get_task_queue()`` 调用**（约定 1）；
- 指标 ``result`` 是封闭枚举 ``{"ok","empty","error"}``，且 prometheus 不可用时的
  ``_NoopMetric`` 兜底表里有这一行（漏了会让指标在降级环境静默消失）；
- 词表在生产侧的判据点（G3）：越界 ``result`` **按原值**进 label 并留
  一条 ``logger.warning``——指标层不改口、不抛异常。

**不连真 Redis**：``redis.asyncio.Redis`` 一律用本文件的 ``_FakeRedis`` 顶替；
**不 sleep 真实长时间**：需要等循环转圈的地方把 ``_CLAIM_INTERVAL_S`` 临时压到毫秒级。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
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

    def test_each_unexpected_shape_hits_the_guard(self, warning_logs):
        """G1：异常出口是**三臂共用一个降级出口**，判据按臂逐一生效——把 ``except``
        元组删窄（哪怕只留 ``KeyError`` 一臂）本用例必须红。三枚输入各自命中一臂：

        - ``("7-0",)`` 只有 1 段，``raw[1]`` 抛 **IndexError**；
        - ``7`` 是真整数（truthy，过得了 ``not raw`` 入口闸），``raw[0]`` 抛 **TypeError**；
        - ``{"oops": 1}`` 下标取 0 而键只有 ``"oops"``，``raw[0]`` 抛 **KeyError**
          （与 ``test_empty_and_unexpected_shapes_degrade_to_no_entries`` 同形、刻意保留：
          那条钉「降级到无条目」这个结果，本条钉「按臂命中守卫」这个判据）。

        三枚输入分三次调用、逐次核对 WARNING **增量计数**而非只看总数——只看总数时
        "某一臂不再记日志"会糊过去（留痕是这条降级支路唯一的可观测面，约定 2）。
        """
        cases = (
            ("IndexError", ("7-0",)),
            ("TypeError", 7),
            ("KeyError", {"oops": 1}),
        )
        for arm, raw in cases:
            before = len(warning_logs)
            assert _normalize_xautoclaim_response(raw) == ("0-0", []), arm
            assert len(warning_logs) == before + 1, f"{arm} 臂必须各记一次 WARNING"

    def test_list_shapes_normalize_like_tuples(self):
        """G8：redis-py 在不同版本/解析路径下透传服务端原始响应的容器**可能是 list 而非
        tuple**；归一化只在 ``_normalize_xautoclaim_response`` 一处（模块 docstring 已声明），
        形态假设不该只钉 tuple 一半。同一批数据分别喂 tuple 与 list 形态（2 段与 3 段两种
        长度，3 段那份带 ``["8-0"]`` 删除位，与 ``test_three_element_shape`` 同源），
        断言两种容器形态结果**逐字相等**。

        末两行 sanity 断言是刻意的：等式两侧若**同时**退化成 ``("0-0", [])``，相等仍成立，
        用例就会假绿——钉住 tuple 侧非空，等价断言才有判别力。
        """
        tuple_2 = ("5-0", _entries(2))
        list_2 = ["5-0", _entries(2)]
        assert _normalize_xautoclaim_response(list_2) == _normalize_xautoclaim_response(tuple_2)

        tuple_3 = ("9-0", _entries(1), ["8-0"])
        list_3 = ["9-0", _entries(1), ["8-0"]]
        assert _normalize_xautoclaim_response(list_3) == _normalize_xautoclaim_response(tuple_3)

        assert _normalize_xautoclaim_response(tuple_2)[1] != [], "sanity：2 段侧不该本来就为空"
        assert _normalize_xautoclaim_response(tuple_3)[0] == "9-0", "sanity：游标侧不该本来就退化"


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
        # Z2：`ok` 由轮级一枚改为**逐条**计数——
        # 这条原本钉住旧形态 `["ok"]`（三条消息只记一次），行为变更 ⇒ 断言
        # 同步更新到新形态，其余断言一字不动。
        assert claim_results == ["ok", "ok", "ok"]
        # 认领参数取自模块常量，且每次只认一轮
        assert len(stub.claim_calls) == 1
        kwargs = stub.claim_calls[0]["kwargs"]
        assert kwargs["min_idle_time"] == _CLAIM_IDLE_MS
        assert kwargs["count"] == _CLAIM_MAX_PER_ROUND
        assert kwargs["start_id"] == "0-0"

    @pytest.mark.asyncio
    async def test_mid_round_failure_keeps_delivered_messages_counted(
        self, monkeypatch, claim_results
    ):
        """Z1（RED 主力）：`ok` 必须**逐条**计数。

        一轮三条、前两条成功交接、第三条抛：抛之前已成功交接的两条**必须**
        已入账（`claim_results == ["ok", "ok"]`），且认领路径**不含** `error`
        ——`error` 仍只属于「XAUTOCLAIM 自身失败」那一枚 except（轮级语义不动）。
        同时钉住既有语义一字不改：异常仍穿出本函数（由 `start_worker` 外层兜），
        认领路径不新增 except 臂、不新增 error 记点。现树（`ok` 在整轮之后才记
        一次）下本用例必红：轮中抛出时循环尾部的记点到不了，已成功消息两头落空
        （现树实测读数 `[]`）。
        """
        entries = _entries(3)
        stub = _FakeRedis(claim_response=("0-0", entries))
        queue = _make_queue(stub)

        seen: List[str] = []

        async def spy(message_id: str, data: Dict[str, str]) -> None:
            seen.append(message_id)
            if len(seen) == 3:
                raise RuntimeError("handler exploded mid-round")

        monkeypatch.setattr(queue, "_process_message", spy)

        with pytest.raises(RuntimeError, match="handler exploded mid-round"):
            await queue._claim_stale_pending()

        assert seen == ["1-0", "2-0", "3-0"], "第三条抛出即穿出，前两条已交接"
        assert claim_results == ["ok", "ok"], "已成功交接的两条必须逐条入账"
        assert "error" not in claim_results, "轮中抛出不是认领失败，不得记 error"

    @pytest.mark.asyncio
    async def test_ok_counts_match_the_per_round_claimed_return(
        self, monkeypatch, claim_results
    ):
        """Z4（口径同源）：指标 `ok` 的逐条计数与 `claimed`（INFO 日志 `count=`
        取的就是它）在同一轮里必须相等——两处「认领到几条」的判定不许漂移
        （约定 6 ④）。用 4 条消息的一轮钉等式：改前形态下 ok 只有一枚、
        返回值却是 4，等式必红。"""
        entries = _entries(4)
        stub = _FakeRedis(claim_response=("0-0", entries))
        queue = _make_queue(stub)

        processed: List[str] = []

        async def spy(message_id: str, data: Dict[str, str]) -> None:
            processed.append(message_id)

        monkeypatch.setattr(queue, "_process_message", spy)

        claimed = await queue._claim_stale_pending()

        assert claimed == 4
        assert processed == ["1-0", "2-0", "3-0", "4-0"]
        assert claim_results.count("ok") == claimed, "ok 逐条计数必须与 claimed 同源"
        assert all(r == "ok" for r in claim_results), "全轮成功时不得混入其他档位"

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

    def test_out_of_vocabulary_result_warns_but_records_verbatim(self, caplog):
        """G3：词表在生产侧第一次成为判据——越界值**按原值**进 label
        并有一条 warning 留痕；词表内取值不产生任何 warning（逐字等价于现树行为）。
        指标层不改口（不落成 ``"other"``）的理由见 ``metrics.py`` 中
        ``TASK_QUEUE_CLAIM_RESULT_VALUES`` 之上的注释。"""

        def _count(result: str) -> float:
            from prometheus_client import REGISTRY

            return REGISTRY.get_sample_value("task_queue_claimed_total", {"result": result}) or 0.0

        with caplog.at_level(logging.WARNING, logger="agentix.observability.metrics"):
            before = _count("ok")
            metrics.record_task_queue_claimed("ok")
            assert _count("ok") == before + 1
            assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
                "词表内取值不得新增日志（flag 语义外的噪声，约定 1）"
            )

            before = _count("bogus")
            metrics.record_task_queue_claimed("bogus")
            assert _count("bogus") == before + 1, "越界必须按原值进 label，不许改口成 other"

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1, "越界只留一条 warning，不抛异常也不静默（约定 2）"
        assert "bogus" in warnings[0].getMessage()

        # G21（U-A）：不可散列实参（原形态在 ``not in`` 就抛 TypeError 上抛主链路）经守卫后
        # 只留痕跳过、不外抛。这一句必须排在上面 `warnings` 断言**之后**——它自己会经守卫
        # 再发一条降级 warning，计入那条断言就不是「恰一条」。
        metrics.record_task_queue_claimed(["not-a-str"])  # type: ignore[arg-type]


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
        assert records == [], "flag 关时一行日志都不该有（约定 1）"

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
        """构造/启动失败只留痕，不阻断启动（约定 2）；返回值 False 表示没有任务可取消。"""
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

    @pytest.mark.asyncio
    async def test_worker_coro_exception_surfaced_by_done_callback(self, monkeypatch):
        """G5：worker 协程**内部**抛异常必须可观测——done-callback 记一条 ERROR（含类型与消息）。

        同步侧构造/启动失败已由 ``test_start_failure_is_fail_open`` 钉住；本用例补的是
        "任务起好了、协程跑起来才炸"这段此前不可见的窗口：error 日志里必须出现回调
        文案（与新实现逐字对榫），且**不得**是同批挪进 try 的那句启动失败文案
        （启动本身成功了）。优雅关停走取消路径不记 error，由既有关停用例覆盖。
        """
        from agentix import main

        class _BoomQueue:
            stream_name = "agentix:tasks"
            consumer_group = "workers"
            consumer_name = "w-test"

            async def start_worker(self) -> None:
                raise RuntimeError("worker coroutine exploded")

        monkeypatch.setattr(settings, "TASK_QUEUE_WORKER_ENABLED", True, raising=False)
        monkeypatch.setattr(tq, "get_task_queue", lambda: _BoomQueue())
        monkeypatch.setattr(main, "_task_queue_worker_task", None)
        monkeypatch.setattr(main, "_task_queue_worker", None)

        records: List[str] = []
        handler_id = logger.add(lambda m: records.append(str(m)), level="ERROR")
        try:
            assert main._start_task_queue_worker() is True
            task = main._task_queue_worker_task
            assert task is not None
            with pytest.raises(RuntimeError):
                await task
            # done-callback 经 call_soon 排队：await 结束后再给循环一拍，确保回调已执行
            await asyncio.sleep(0)
        finally:
            logger.remove(handler_id)

        joined = "\n".join(records)
        assert "task queue worker crashed" in joined, "协程内异常必须由回调记为 ERROR"
        assert "RuntimeError" in joined, "留痕必须带异常类型"
        assert "worker coroutine exploded" in joined, "留痕必须带异常消息"
        assert "task queue worker start failed" not in joined, "启动本身成功，不该报同步侧文案"


class TestSingleSourceOfDecision:
    def test_start_worker_does_not_read_the_flag(self):
        """「要不要跑」只在 `_start_task_queue_worker` 判一次（约定 6 ④）。"""
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
