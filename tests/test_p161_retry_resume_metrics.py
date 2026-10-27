"""LG-3-3 验收：四个业务指标（重试三层 + 图续跑四入口）。

## 验收口径（指导文档 §4.4 LG-3-3「怎么算过」）

> 每层都能观测到；label 值全部有界（``layer`` 是枚举、``kind`` 是枚举）；
> **不得引入 thread_id / user_id 等高基数 label**。

本文件怎么钉这三条：

1. **每层都能观测到**——不是读源码里的字符串，而是让 tool / task / retrieval
   三层真的走一次重试（真实 ``ToolExecutor`` / ``run_task`` /
   ``retrieve_docs_with_retry``），从 Prometheus 注册表读回增量；四个续跑
   入口同样各读一次增量。
2. **label 值有界**——``record_*`` 对枚举外的值一律落 ``"other"``（越界串
   不会新开时间序列）；生产调用点的字面量用 AST 扫，必须与封闭枚举**双向**
   一致（多一个值、少一个值都红）。
3. **没有高基数 label**——四个指标的 label 只有 ``layer`` / ``kind``，且都不在
   ``test_p100_genai_observability.py::FORBIDDEN_METRIC_LABELS`` 里。

**口径说明**：``retry_attempts_total`` 只数**重试**（首次尝试不计）；
``retry_sleep_seconds_total`` 只累计**真的停了**的退避（预算截断/拒批不计）；
``retry_exhausted_total`` 只数「不会再有下一次尝试」的失败——首次即判不可重试
的错误从未进入重试，不算耗尽。
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import time
from typing import Any, Dict, List

import pytest
from prometheus_client import REGISTRY

from agentix.application.services import task_retry
from agentix.application.services.breakpoint_resume import CheckpointRecovery
from agentix.application.services.task_plan import TaskSpec
from agentix.config import settings
from agentix.observability import metrics
from agentix.retrieval.usecases import chat_service
from agentix.tools.definitions import ToolSpec
from agentix.tools.gateway.executor import ToolExecutor

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]

RETRY_METRICS = (
    ("retry_attempts", "layer", metrics.RETRY_LAYER_VALUES),
    ("retry_sleep_seconds", "layer", metrics.RETRY_LAYER_VALUES),
    ("retry_exhausted", "layer", metrics.RETRY_LAYER_VALUES),
    ("graph_resume", "kind", metrics.GRAPH_RESUME_KIND_VALUES),
)


def _read(sample: str, **labels: str) -> float:
    value = REGISTRY.get_sample_value(sample, labels)
    return 0.0 if value is None else value


def _layer_metrics(layer: str) -> tuple:
    """``(attempts, sleep_seconds, exhausted)`` 当前累计值。"""
    return (
        _read("retry_attempts_total", layer=layer),
        _read("retry_sleep_seconds_total", layer=layer),
        _read("retry_exhausted_total", layer=layer),
    )


def _diff(after: tuple, before: tuple) -> tuple:
    return tuple(round(a - b, 6) for a, b in zip(after, before))


def _record_sleeps(monkeypatch) -> List[float]:
    sleeps: List[float] = []

    async def _record(delay, *args, **kwargs):
        sleeps.append(delay)
        return None

    monkeypatch.setattr(asyncio, "sleep", _record)
    return sleeps


# ---------------------------------------------------------------------------
# 1. 定义面：指标名、label 名与有界标签
# ---------------------------------------------------------------------------


class TestMetricDefinitions:
    @pytest.mark.parametrize(("name", "label", "_values"), RETRY_METRICS)
    def test_metric_exists_with_exactly_one_bounded_label(self, name, label, _values):
        assert metrics.PROMETHEUS_AVAILABLE, "测试环境缺 prometheus_client，本组用例失去意义"
        metric = getattr(metrics, name.upper())
        assert metric._name == name, f"{name} 的指标名漂了：{metric._name}"
        assert metric._labelnames == (label,), (
            f"{name} 的 label 应只有 ({label},)，实得 {metric._labelnames}"
        )

    def test_labels_are_whitelisted_and_never_forbidden(self):
        from tests.test_p100_genai_observability import (
            ALLOWED_METRIC_LABELS,
            FORBIDDEN_METRIC_LABELS,
        )

        for _name, label, _values in RETRY_METRICS:
            assert label in ALLOWED_METRIC_LABELS, f"{label} 未进 p100 白名单"
            assert label not in FORBIDDEN_METRIC_LABELS, f"{label} 是高基数/隐私标签"

    def test_enum_values_are_bounded_frozensets(self):
        assert metrics.RETRY_LAYER_VALUES == frozenset({"tool", "task", "retrieval"})
        assert metrics.GRAPH_RESUME_KIND_VALUES == frozenset(
            {"startup", "ops", "retry_task", "hitl_in_loop"}
        )


# ---------------------------------------------------------------------------
# 2. label 取值有界：越界落 "other"，调用点与枚举双向一致
# ---------------------------------------------------------------------------


class TestLabelValuesAreBounded:
    def test_out_of_enum_retry_layer_lands_in_other(self):
        before = _layer_metrics("other")
        metrics.record_retry_attempt("tenant-xyz-123")
        metrics.record_retry_sleep("tenant-xyz-123", 0.5)
        metrics.record_retry_exhausted("tenant-xyz-123")
        assert _diff(_layer_metrics("other"), before) == (1, 0.5, 1)
        assert REGISTRY.get_sample_value(
            "retry_attempts_total", {"layer": "tenant-xyz-123"}
        ) is None, "越界 layer 新开了时间序列——基数闸失效"

    def test_out_of_enum_resume_kind_lands_in_other(self):
        before = _read("graph_resume_total", kind="other")
        metrics.record_graph_resume("thread-abc")
        assert _read("graph_resume_total", kind="other") - before == 1
        assert REGISTRY.get_sample_value(
            "graph_resume_total", {"kind": "thread-abc"}
        ) is None, "越界 kind 新开了时间序列——基数闸失效"


def _literals(path: pathlib.Path, func_name: str, keyword: str | None = None) -> set:
    """扫一个文件里 ``func_name`` 调用上的常量取值（位置参数或关键字）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != func_name:
            continue
        if keyword is None:
            if node.args and isinstance(node.args[0], ast.Constant):
                found.add(node.args[0].value)
        else:
            for kw in node.keywords:
                if kw.arg == keyword and isinstance(kw.value, ast.Constant):
                    found.add(kw.value.value)
    return found


def _production_literals(func_name: str, keyword: str | None = None) -> set:
    found = set()
    for path in (PROJECT_ROOT / "agentix").rglob("*.py"):
        found |= _literals(path, func_name, keyword)
    return found


class TestCallSitesMatchTheEnum:
    def test_retry_layers_used_at_call_sites_match_enum(self):
        sites = set()
        for func in ("record_retry_attempt", "record_retry_sleep", "record_retry_exhausted"):
            sites |= _production_literals(func)
        sites |= _production_literals("try_consume", "layer")
        assert sites == metrics.RETRY_LAYER_VALUES, (
            f"调用点与枚举不一致：sites={sorted(sites)} "
            f"enum={sorted(metrics.RETRY_LAYER_VALUES)}"
        )

    def test_resume_kinds_used_at_call_sites_match_enum(self):
        sites = _production_literals("record_graph_resume")
        sites |= _production_literals("resume_thread", "kind")
        assert sites == metrics.GRAPH_RESUME_KIND_VALUES, (
            f"续跑入口与枚举不一致：sites={sorted(sites)} "
            f"enum={sorted(metrics.GRAPH_RESUME_KIND_VALUES)}"
        )


# ---------------------------------------------------------------------------
# 3. 行为面：三层重试各自可观测
# ---------------------------------------------------------------------------


class _FailingStore:
    """只实现 ``similarity_search``（走 chat_service 的回退分支）并总是抛错。"""

    index = None

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    def similarity_search(self, question, **kwargs):
        self.calls += 1
        raise self.exc


class TestRetryLayersAreObservable:
    @pytest.mark.asyncio
    async def test_tool_layer_retry_increments_all_three(self, monkeypatch):
        sleeps = _record_sleeps(monkeypatch)
        before = _layer_metrics("tool")

        async def always_fails(args):
            raise ConnectionResetError("down")

        executor = ToolExecutor()
        executor.register_handler(
            "lg33_flaky",
            always_fails,
            spec=ToolSpec(
                name="lg33_flaky", description="d", retryable=True, max_retries=2
            ),
        )
        result = await executor.execute("lg33_flaky", {})

        attempts, sleep_s, exhausted = _diff(_layer_metrics("tool"), before)
        assert result.success is False
        assert attempts == 2, f"工具层实际重试 2 次，指标记 {attempts}"
        assert sleep_s == pytest.approx(sum(sleeps)), (
            f"退避秒数总量应等于真实停等之和 {sleeps}，实得 {sleep_s}"
        )
        assert exhausted == 1

    @pytest.mark.asyncio
    async def test_tool_layer_non_retryable_failure_is_not_exhaustion(self, monkeypatch):
        monkeypatch.setattr(settings, "RETRY_BUDGET_ENABLED", True)
        sleeps = _record_sleeps(monkeypatch)
        before = _layer_metrics("tool")

        async def bad_request(args):
            raise ValueError("bad args")

        executor = ToolExecutor()
        executor.register_handler(
            "lg33_bad",
            bad_request,
            spec=ToolSpec(name="lg33_bad", description="d", retryable=True, max_retries=2),
        )
        result = await executor.execute("lg33_bad", {})

        assert result.success is False
        assert sleeps == [], "不可重试错误不得退避"
        assert _diff(_layer_metrics("tool"), before) == (0, 0, 0), (
            "首错即判不可重试从未进入重试，不该记 attempts/sleep/exhausted"
        )

    @pytest.mark.asyncio
    async def test_task_layer_retry_increments_all_three(self):
        async def runner(spec):
            raise ConnectionResetError("reset")

        before = _layer_metrics("task")
        outcome = await task_retry.run_task(
            TaskSpec("t"), runner, max_attempts=3, base_delay=0.01, jitter=False
        )

        attempts, sleep_s, exhausted = _diff(_layer_metrics("task"), before)
        assert outcome.attempts == 3
        assert attempts == 2
        assert sleep_s == pytest.approx(0.01 + 0.02)
        assert exhausted == 1

    @pytest.mark.asyncio
    async def test_task_layer_non_retryable_failure_is_not_exhaustion(self):
        async def runner(spec):
            raise ValueError("bad")

        before = _layer_metrics("task")
        outcome = await task_retry.run_task(
            TaskSpec("t"), runner, max_attempts=3, base_delay=0.01, jitter=False
        )

        assert outcome.attempts == 1
        assert outcome.retryable is False
        assert _diff(_layer_metrics("task"), before) == (0, 0, 0)

    @pytest.mark.asyncio
    async def test_task_layer_budget_denial_counts_as_exhausted(self, monkeypatch):
        monkeypatch.setattr(settings, "RETRY_BUDGET_ENABLED", True)
        import agentix.reliability.retry_budget as retry_budget_module

        monkeypatch.setattr(
            retry_budget_module, "ensure_retry_budget", lambda: retry_budget_module.RetryBudget(0)
        )

        async def runner(spec):
            raise ConnectionResetError("reset")

        before = _layer_metrics("task")
        outcome = await task_retry.run_task(
            TaskSpec("t"), runner, max_attempts=3, base_delay=0.01, jitter=False
        )

        attempts, sleep_s, exhausted = _diff(_layer_metrics("task"), before)
        assert outcome.attempts == 1
        assert (attempts, sleep_s, exhausted) == (0, 0.0, 1), (
            "预算拒批：没有发生重试与停等，但重试链确实被耗尽"
        )

    def test_retrieval_layer_retry_increments_all_three(self, monkeypatch):
        sleeps: List[float] = []
        monkeypatch.setattr(time, "sleep", lambda delay: sleeps.append(delay))
        monkeypatch.setattr(settings, "RETRY_BUDGET_ENABLED", False)
        store = _FailingStore(ConnectionResetError("reset"))

        before = _layer_metrics("retrieval")
        with pytest.raises(ConnectionResetError):
            chat_service.retrieve_docs_with_retry(store, "q", search_k=4, max_retries=2)

        attempts, sleep_s, exhausted = _diff(_layer_metrics("retrieval"), before)
        assert store.calls == 3
        assert attempts == 2
        assert sleeps == [0.5, 1.0], f"检索层退避序列变了：{sleeps}"
        assert sleep_s == pytest.approx(1.5)
        assert exhausted == 1

    def test_retrieval_non_retryable_failure_is_not_exhaustion(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda delay: None)
        monkeypatch.setattr(settings, "RETRY_BUDGET_ENABLED", True)
        store = _FailingStore(ValueError("bad"))

        before = _layer_metrics("retrieval")
        with pytest.raises(ValueError):
            chat_service.retrieve_docs_with_retry(store, "q", search_k=4, max_retries=2)

        assert store.calls == 1
        assert _diff(_layer_metrics("retrieval"), before) == (0, 0, 0)


# ---------------------------------------------------------------------------
# 4. 行为面：四个续跑入口各自可观测
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self) -> None:
        self.store: Dict[str, Any] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


class _ResumeGraphStub:
    def __init__(self) -> None:
        self.inputs: List[Any] = []

    def get_state(self, config):
        return type("S", (), {"values": {"messages": []}})()

    async def ainvoke(self, input, config=None, **kwargs):
        self.inputs.append(input)
        return {"messages": []}


class TestGraphResumeKindsAreObservable:
    @pytest.mark.parametrize("kind", ["startup", "ops", "retry_task"])
    def test_resume_thread_counts_the_declared_kind(self, kind):
        before = _read("graph_resume_total", kind=kind)
        graph = _ResumeGraphStub()
        result = asyncio.run(
            CheckpointRecovery(redis_client=_FakeRedis()).resume_thread("t-1", graph, kind=kind)
        )
        assert result is not None
        assert graph.inputs == [None], "没有真的发起续跑，指标却在自夸"
        assert _read("graph_resume_total", kind=kind) - before == 1

    def test_in_loop_resume_counts_hitl_kind(self, monkeypatch):
        from agentix.orchestration.langgraph import lg_builder

        class _GraphStub:
            async def ainvoke(self, input, config=None, **kwargs):
                return {}

        monkeypatch.setattr(lg_builder, "graph", _GraphStub())
        before = _read("graph_resume_total", kind="hitl_in_loop")
        asyncio.run(lg_builder.resume_in_loop("t-2", "approve"))
        assert _read("graph_resume_total", kind="hitl_in_loop") - before == 1

    def test_no_resume_attempted_means_no_counter(self):
        """无 checkpoint 时 return None——没向图发起续跑，不得计数。"""
        class _NoCheckpointGraph:
            def get_state(self, config):
                return type("S", (), {"values": {}})()

        before = _read("graph_resume_total", kind="ops")
        result = asyncio.run(
            CheckpointRecovery(redis_client=_FakeRedis()).resume_thread(
                "ghost", _NoCheckpointGraph(), kind="ops"
            )
        )
        assert result is None
        assert _read("graph_resume_total", kind="ops") - before == 0
