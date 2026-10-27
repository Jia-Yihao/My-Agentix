"""LG-3-1：退避停等的 deadline 预算感知（三层共用一个判定）。

覆盖面与验收口径（指导文档 §4.3 LG-3-1）：
- **算术只有一份**：``retry.budgeted_pause(delay, remaining)`` 给出
  ``(实际停等, 是否放弃)``——无 deadline ⇒ ``(delay, False)``；
  预算够 ⇒ 原样；预算不够 ⇒ ``(remaining, True)``（停等压进剩余，不再开下一次）；
- **三层都接上**：检索（``aretry``）/ task（``run_task``）/ 工具（``ToolExecutor``）
  在有 deadline 且余量不足时**不进入下一次尝试**，并把该次结果标记为
  ``truncated``（检索层在抛出的异常上打 ``retry_truncated`` 标）；
- **剩余 1s 时不进入第 3 次尝试**（指导文档的验收原话）：三层各钉一条；
- **无 deadline 时与基线逐位等价**：三层都仍跑满 ``max_attempts``，且不出现
  truncated 标记；
- **deadline 的来源唯一**：``RequestScope.deadline_at``——application 在导入期
  把读取函数注册进 domain（依赖倒置），domain 不反向 import application；
  provider 故障按「无 deadline」降级（纪律 2）。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

import agentix.domain.policies.retry as retry_policy
from agentix.application.services import request_scope as scope_module
from agentix.application.services.request_scope import request_scope
from agentix.application.services.task_plan import TaskSpec
from agentix.application.services.task_retry import run_task
from agentix.domain.policies.retry import aretry, budgeted_pause, is_retry_truncated
from agentix.domain.runtime.lifecycle import ExitReason
from agentix.tools.definitions import ToolSpec
from agentix.tools.gateway.executor import ToolExecutor

#: 模块导入 ``request_scope`` 时注册的 provider——各用例改完必须还回去。
_REGISTERED_PROVIDER = retry_policy._remaining_provider


@pytest.fixture(autouse=True)
def _restore_provider():
    try:
        yield
    finally:
        retry_policy.set_remaining_provider(_REGISTERED_PROVIDER)


def _use_remaining(*values: Optional[float]) -> List[int]:
    """把 provider 换成按序取值的桩（取完后重复最后一项），返回调用计数。"""
    seq = list(values)
    calls: List[int] = []

    def _provider() -> Optional[float]:
        calls.append(1)
        return seq[min(len(calls) - 1, len(seq) - 1)]

    retry_policy.set_remaining_provider(_provider)
    return calls


# ---------------------------------------------------------------------------
# 1. 算术：budgeted_pause 的四种取值
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("delay", "remaining", "expected"),
    [
        (5.0, None, (5.0, False)),  # 无 deadline ⇒ 基线
        (0.5, 2.0, (0.5, False)),  # 预算够 ⇒ 原样
        (2.0, 0.5, (0.5, True)),  # 预算不够 ⇒ 只等剩余并放弃
        (0.5, 0.5, (0.5, True)),  # 恰好相等也放弃（停满即越过 deadline）
        (1.0, 0.0, (0.0, True)),  # 预算已耗尽 ⇒ 不等、立即放弃
    ],
)
def test_budgeted_pause(delay, remaining, expected):
    assert budgeted_pause(delay, remaining) == expected


# ---------------------------------------------------------------------------
# 2. deadline 来源：唯一的 provider，故障按「无 deadline」降级
# ---------------------------------------------------------------------------


def test_remaining_seconds_without_provider_is_none():
    retry_policy.set_remaining_provider(None)
    assert retry_policy.remaining_seconds() is None


def test_remaining_seconds_without_deadline_is_none():
    retry_policy.set_remaining_provider(_REGISTERED_PROVIDER)

    async def _check():
        async with request_scope():
            return retry_policy.remaining_seconds()

    assert asyncio.run(_check()) is None


def test_registered_provider_reads_scope_deadline():
    retry_policy.set_remaining_provider(_REGISTERED_PROVIDER)

    async def _check():
        async with request_scope() as scope:
            scope.set_deadline_seconds(5)
            return retry_policy.remaining_seconds()

    remaining = asyncio.run(_check())
    assert remaining is not None and 4.0 < remaining <= 5.0
    # 退出作用域后回到「无 deadline」（provider 读的是当前 context）
    assert retry_policy.remaining_seconds() is None


def test_provider_failure_degrades_to_no_deadline():
    def _boom() -> Optional[float]:
        raise RuntimeError("scope 已被回收")

    retry_policy.set_remaining_provider(_boom)
    assert retry_policy.remaining_seconds() is None


def test_request_scope_registers_the_provider_at_import():
    """注册点是 application 的 request_scope（依赖倒置），不是各调用方各猜一次。"""
    assert scope_module.__name__.endswith("request_scope")
    assert callable(_REGISTERED_PROVIDER)


# ---------------------------------------------------------------------------
# 3. 检索层：aretry
# ---------------------------------------------------------------------------


def test_aretry_gives_up_when_budget_cannot_cover_backoff():
    """剩余 1s、退避 0.01/0.02s：第 2 次失败后预算只剩 1ms ⇒ 不进入第 3 次尝试。"""
    calls = _use_remaining(1.0, 0.001)
    attempts: List[int] = []

    @aretry(max_attempts=3, base_delay=0.01, max_delay=1.0, jitter=False)
    async def flaky():
        attempts.append(1)
        raise TimeoutError("upstream timeout")

    with pytest.raises(TimeoutError) as ei:
        asyncio.run(flaky())

    assert len(attempts) == 2, "预算不足时仍进入了第 3 次尝试"
    assert len(calls) == 2, "每次失败都该读一次剩余预算（判定只有一个源）"
    assert is_retry_truncated(ei.value)
    # 只打标、不改类型与消息：上层错误分类不受影响
    assert type(ei.value) is TimeoutError and "upstream timeout" in str(ei.value)


def test_aretry_without_deadline_keeps_baseline_attempts():
    retry_policy.set_remaining_provider(None)
    attempts: List[int] = []

    @aretry(max_attempts=3, base_delay=0.01, max_delay=1.0, jitter=False)
    async def flaky():
        attempts.append(1)
        raise TimeoutError("upstream timeout")

    with pytest.raises(TimeoutError) as ei:
        asyncio.run(flaky())

    assert len(attempts) == 3  # 与基线一致：跑满 max_attempts
    assert not is_retry_truncated(ei.value)


# ---------------------------------------------------------------------------
# 4. task 层：run_task
# ---------------------------------------------------------------------------


def _spec() -> TaskSpec:
    return TaskSpec(name="lg31-task")


def test_task_retry_truncated_by_budget():
    _use_remaining(1.0, 0.001)
    attempts: List[int] = []

    async def runner(spec):
        attempts.append(1)
        raise TimeoutError("upstream timeout")

    outcome = asyncio.run(
        run_task(_spec(), runner, max_attempts=3, base_delay=0.01, max_delay=1.0, jitter=False)
    )

    assert outcome.ok is False
    assert outcome.attempts == 2, "预算不足时仍进入了第 3 次尝试"
    assert outcome.truncated is True
    assert outcome.exit_reason is ExitReason.MAX_ITERATIONS
    assert outcome.error is not None and "TimeoutError" in outcome.error


def test_task_retry_without_deadline_keeps_baseline_attempts():
    retry_policy.set_remaining_provider(None)
    attempts: List[int] = []

    async def runner(spec):
        attempts.append(1)
        raise TimeoutError("upstream timeout")

    outcome = asyncio.run(
        run_task(_spec(), runner, max_attempts=3, base_delay=0.01, max_delay=1.0, jitter=False)
    )

    assert outcome.ok is False
    assert outcome.attempts == 3  # 与基线一致
    # 既有语义不变：可重试错误耗尽 max_attempts ⇒ truncated（LG-3-1 前就如此）
    assert outcome.truncated is True


# ---------------------------------------------------------------------------
# 5. 工具层：ToolExecutor
# ---------------------------------------------------------------------------

_TOOL_NAME = "lg31_budget_probe"


def _tool_spec() -> ToolSpec:
    return ToolSpec(
        name=_TOOL_NAME,
        description="probe 何时用：LG-3-1 预算截断用例；何时不能用：生产",
        risk_level="L0",
        retryable=True,
        max_retries=2,
        timeout_ms=5_000,
    )


def _tool_executor(counter: List[int]):
    async def handler(args: Dict[str, Any]):
        counter.append(1)
        raise TimeoutError("upstream timeout")

    executor = ToolExecutor()
    executor.register_handler(_TOOL_NAME, handler, spec=_tool_spec())
    return executor


def test_tool_retry_truncated_by_budget():
    _use_remaining(1.0, 0.001)
    counter: List[int] = []

    result = asyncio.run(_tool_executor(counter).execute(_TOOL_NAME, {}))

    assert result.success is False
    assert len(counter) == 2, "预算不足时仍进入了第 3 次尝试（1 次执行 + 1 次重试）"
    assert result.truncated is True
    assert result.error is not None and "timeout" in result.error.lower()


def test_tool_retry_without_deadline_keeps_baseline_attempts():
    retry_policy.set_remaining_provider(None)
    counter: List[int] = []

    result = asyncio.run(_tool_executor(counter).execute(_TOOL_NAME, {}))

    assert result.success is False
    assert len(counter) == 3  # 与基线一致：1 + max_retries
    assert result.truncated is False
