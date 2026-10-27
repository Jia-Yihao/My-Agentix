"""LG-2-7：启动恢复加墙钟与并发上限（``main._startup_crash_recovery``）。

覆盖面与验收口径（指导文档 §4.3 LG-2-7）：
- **不随 N 线性增长**：待恢复线程超过并发上限时，启动耗时按「批数」而不是
  「条数」增长（3 条 × 150ms、上限 2 ⇒ 两批，串行会是三批）；并发峰值恰为
  ``RECOVERY_MAX_CONCURRENCY``；
- **单个卡死不会拖垮启动**：卡死线程受 ``AGENT_TIMEOUT`` 约束，被超时取消后
  其余线程照常恢复；
- **总预算**：整体（扫描 + 恢复）受 ``RECOVERY_TOTAL_BUDGET_S`` 约束，预算
  耗尽即返回，未恢复的线程留在未完成态等下一次启动；
- **既有语义不回退**：挂起中断的线程仍跳过（LG-2-2），续跑返回 None 不计数，
  扫描异常只留痕不阻断启动（纪律 2），扫描的 60 秒空闲闸不变（LG-2-1）；
- **接线**：``_startup`` 真的调用 ``_startup_crash_recovery``（AST 判定）。
"""

from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from agentix import main as main_module
from agentix.application.services import breakpoint_resume as br
from agentix.application.services.breakpoint_resume import ThreadInterruptedError
from agentix.config import settings
from agentix.orchestration.langgraph import lg_builder

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _FakeRecovery:
    """记录并发峰值、实际使用的 graph、扫描闸口与每线程结局。"""

    def __init__(
        self,
        thread_ids: List[str],
        *,
        delays: Dict[str, float] | None = None,
        interrupted: set | None = None,
        fail: set | None = None,
    ):
        self.thread_ids = thread_ids
        self.delays = delays or {}
        self.interrupted = interrupted or set()
        self.fail = fail or set()
        self.active = 0
        self.peak = 0
        self.started: List[str] = []
        self.graphs: List[Any] = []
        self.min_idle_seconds: float | None = None
        self.scan_error: Exception | None = None

    async def get_active_threads(self, graph, *, min_idle_seconds: float = 0.0):
        if self.scan_error is not None:
            raise self.scan_error
        self.min_idle_seconds = min_idle_seconds
        self.graphs.append(graph)
        return list(self.thread_ids)

    async def resume_thread(self, thread_id: str, graph, *, kind: str = "ops"):
        # LG-3-3：生产调用点按入口传 kind（启动恢复 = "startup"）；并发边界用例
        # 不关心入口分型，接住该关键字即可。
        self.started.append(thread_id)
        self.graphs.append(graph)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delays.get(thread_id, 0.0))
        finally:
            self.active -= 1
        if thread_id in self.interrupted:
            raise ThreadInterruptedError(thread_id, 1)
        return None if thread_id in self.fail else {"messages": []}


def _patch(monkeypatch, recovery: _FakeRecovery) -> None:
    monkeypatch.setattr(br, "get_checkpoint_recovery", lambda: recovery)


def _elapsed(coro) -> float:
    t0 = time.monotonic()
    asyncio.run(coro)
    return time.monotonic() - t0


# ---------------------------------------------------------------------------
# 1. 并发上限：耗时按批数增长，峰值等于上限
# ---------------------------------------------------------------------------


def test_three_threads_run_in_two_batches(monkeypatch):
    fake = _FakeRecovery([f"t{i}" for i in range(3)], delays={f"t{i}": 0.15 for i in range(3)})
    _patch(monkeypatch, fake)

    elapsed = _elapsed(main_module._startup_crash_recovery())

    assert fake.peak == main_module.RECOVERY_MAX_CONCURRENCY == 2
    # 串行三批 ≥ 0.45s；两批 ≈ 0.30s。上限给足抖动余量，仍能区分两者。
    assert elapsed < 0.45, f"3 条线程按批并发仍用了 {elapsed:.3f}s（疑似串行）"
    assert sorted(fake.started) == ["t0", "t1", "t2"]


def test_duration_grows_with_batches_not_threads(monkeypatch):
    fake = _FakeRecovery([f"t{i}" for i in range(6)], delays={f"t{i}": 0.1 for i in range(6)})
    _patch(monkeypatch, fake)

    elapsed = _elapsed(main_module._startup_crash_recovery())

    assert fake.peak == 2
    # 6 条 ÷ 2 = 3 批 ≈ 0.30s；串行会是 0.60s。
    assert elapsed < 0.55, f"6 条线程用了 {elapsed:.3f}s（疑似串行）"
    assert len(fake.started) == 6


# ---------------------------------------------------------------------------
# 2. 墙钟：单条卡死不拖垮整轮；总预算封顶
# ---------------------------------------------------------------------------


def test_hung_thread_does_not_block_others(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_TIMEOUT", 0.2)
    fake = _FakeRecovery(
        ["hung", "ok1", "ok2"],
        delays={"hung": 60.0, "ok1": 0.01, "ok2": 0.01},
    )
    _patch(monkeypatch, fake)

    elapsed = _elapsed(main_module._startup_crash_recovery())

    assert "hung" in fake.started
    assert elapsed < 3.0, f"卡死线程拖住了整轮启动（{elapsed:.3f}s）"
    assert fake.peak <= 2


def test_total_budget_caps_whole_recovery(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_TIMEOUT", 300)
    monkeypatch.setattr(main_module, "RECOVERY_TOTAL_BUDGET_S", 0.2)
    fake = _FakeRecovery(["a", "b", "c"], delays={"a": 60.0, "b": 60.0, "c": 60.0})
    _patch(monkeypatch, fake)

    elapsed = _elapsed(main_module._startup_crash_recovery())

    assert elapsed < 2.0, f"总预算失效（{elapsed:.3f}s）"
    # 被取消的续跑不计数（也说明没有把它当成「已恢复」）
    assert fake.active == 0


# ---------------------------------------------------------------------------
# 3. 既有语义：挂起跳过 / None 不计数 / 扫描异常降级 / 60 秒闸 / 当前图
# ---------------------------------------------------------------------------


def test_interrupted_and_none_results_are_not_counted(monkeypatch):
    fake = _FakeRecovery(
        ["gate", "crashed", "ok"],
        interrupted={"gate"},
        fail={"crashed"},
    )
    _patch(monkeypatch, fake)

    asyncio.run(main_module._startup_crash_recovery())

    assert sorted(fake.started) == ["crashed", "gate", "ok"]
    assert fake.min_idle_seconds == 60


def test_scan_failure_does_not_break_startup(monkeypatch):
    fake = _FakeRecovery(["a"])
    fake.scan_error = RuntimeError("redis down")
    _patch(monkeypatch, fake)

    asyncio.run(main_module._startup_crash_recovery())  # 不抛即通过

    assert fake.started == []


def test_recovery_reads_current_graph_module_attribute(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(lg_builder, "graph", sentinel)
    fake = _FakeRecovery(["t1"])
    _patch(monkeypatch, fake)

    asyncio.run(main_module._startup_crash_recovery())

    assert fake.graphs and all(g is sentinel for g in fake.graphs)


# ---------------------------------------------------------------------------
# 4. 接线与常量
# ---------------------------------------------------------------------------


def test_constants_documented():
    assert main_module.RECOVERY_MAX_CONCURRENCY == 2
    assert main_module.RECOVERY_TOTAL_BUDGET_S > 0


def _call_names(node: ast.AST) -> set:
    names = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def test_startup_invokes_crash_recovery():
    tree = ast.parse((PROJECT_ROOT / "agentix/main.py").read_text(encoding="utf-8"))
    startup = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_startup"
    )
    assert "_startup_crash_recovery" in _call_names(startup)


@pytest.mark.parametrize("name", ["_recover_one_thread", "_recover_threads"])
def test_helpers_are_module_level(name):
    assert callable(getattr(main_module, name))
