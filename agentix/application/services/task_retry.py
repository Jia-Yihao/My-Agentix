"""子任务的**局部重试**与失败隔离（部分成功语义）。

《My Agentix_架构优化方案》§9.1 A 组缺口「子任务可独立验证 + 失败局部重试」：
现状「可独立」已达成，「局部重试」只做到**检索通道级**
（`kb_retrieval.py` 的 `@aretry`），**多工具 task 级仍无**。

**为什么 task 级重试不能省**：一次 KG 问答会 fan-out 出多个工具 task
（cypher / graphrag / text2sql）。其中一路的瞬时失败若直接上抛：

- 用户得到**整体失败**，但另外两路其实已经成功——**已付出的算力被丢弃**；
- 失败被放大到请求级，重试代价是整条链路。

本模块给出两件事：

1. :func:`run_task` —— 单任务：**局部**重试（复用 ``domain.policies.aretry``
   的可重试性判定），耗尽后**不抛异常**，而是返回带失败原因的
   :class:`TaskOutcome`（把「失败」变成**数据**，由调用方决定如何呈现）。
2. :func:`run_batch` —— 批任务：并发执行 + **部分成功**语义
   （一路失败不影响其余），并给出 ``partial_success`` 判定。

**与 ``aretry`` 的分工**：``aretry`` 是**装饰器**（失败继续抛，交给上层）；
本模块是**边界**（失败转成结果对象），二者组合使用——重试策略复用，不重写。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from loguru import logger

from agentix.application.services.task_plan import TaskSpec
from agentix.config import settings
from agentix.domain.policies.error_classification import is_retryable
from agentix.domain.policies.retry import (
    RETRY_MAX_ATTEMPTS,
    RetryBackoff,
    budgeted_pause,
    remaining_seconds,
)
from agentix.domain.runtime.lifecycle import ExitReason, ExitStatus
from agentix.observability.metrics import (
    record_retry_attempt,
    record_retry_exhausted,
    record_retry_sleep,
)

__all__ = [
    "TaskOutcome",
    "BatchOutcome",
    "run_task",
    "run_batch",
    "DEFAULT_MAX_ATTEMPTS",
    "task_retry_backoff",
]

#: 默认最大尝试次数（含首次）；与原生 ``RetryPolicy`` 默认同源（LG-1-3）
DEFAULT_MAX_ATTEMPTS = RETRY_MAX_ATTEMPTS

#: flag 关时的历史退避上界（LG-1-3 前 task 层的实值）
_LEGACY_TASK_MAX_INTERVAL = 8.0


def task_retry_backoff() -> RetryBackoff:
    """task 层重试退避形状（``RETRY_BACKOFF_ALIGNED`` 开 ⇒ 与原生默认逐项相同）。

    ``max_attempts`` 只是快照的一部分：``run_task`` 的实际尝试次数仍由调用方
    的 ``max_attempts`` 形参决定（``run_batch`` 会透传），开关不改调用方语义。
    """
    if bool(getattr(settings, "RETRY_BACKOFF_ALIGNED", False)):
        return RetryBackoff()
    return RetryBackoff(max_interval=_LEGACY_TASK_MAX_INTERVAL)

#: 任务执行体签名
TaskRunner = Callable[[TaskSpec], Awaitable[Any]]


@dataclass
class TaskOutcome:
    """单个任务的执行结果（**失败也是结果**，不抛异常）。"""

    name: str
    ok: bool
    value: Any = None
    error: Optional[str] = None
    error_type: Optional[str] = None
    attempts: int = 0
    retryable: bool = False
    duration_ms: float = 0.0
    skipped_reason: Optional[str] = None
    #: 统一退出契约（A1b；``None`` = 按 ok/retryable 在 ``__post_init__`` 派生）
    exit_status: Optional[ExitStatus] = None
    exit_reason: Optional[ExitReason] = None
    #: 预算/重试耗尽导致的截断（retryable 失败耗尽预算 ⇒ True）
    truncated: bool = False

    def __post_init__(self) -> None:
        """派生退出契约（显式传入的字段不覆盖；手工构造同样满足契约）。"""
        if self.exit_status is None:
            if self.ok:
                self.exit_status = ExitStatus.COMPLETED
                if self.exit_reason is None:
                    self.exit_reason = ExitReason.COMPLETED
            elif self.retryable:
                # 可重试错误走到返回 = 重试预算耗尽（max_iterations）
                self.exit_status = ExitStatus.FAILED
                if self.exit_reason is None:
                    self.exit_reason = ExitReason.MAX_ITERATIONS
                self.truncated = True
            else:
                self.exit_status = ExitStatus.FAILED
                if self.exit_reason is None:
                    self.exit_reason = ExitReason.ERROR
        elif self.exit_reason is None:
            self.exit_reason = ExitReason.COMPLETED if self.ok else ExitReason.ERROR

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "error": self.error,
            "error_type": self.error_type,
            "attempts": self.attempts,
            "retryable": self.retryable,
            "duration_ms": round(self.duration_ms, 2),
            "skipped_reason": self.skipped_reason,
            "exit_status": self.exit_status.value if self.exit_status else "",
            "exit_reason": self.exit_reason.value if self.exit_reason else "",
            "truncated": self.truncated,
        }


@dataclass
class BatchOutcome:
    """批任务的执行结果（部分成功语义）。"""

    outcomes: List[TaskOutcome] = field(default_factory=list)

    @property
    def succeeded(self) -> List[TaskOutcome]:
        return [o for o in self.outcomes if o.ok]

    @property
    def failed(self) -> List[TaskOutcome]:
        return [o for o in self.outcomes if not o.ok]

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def partial_success(self) -> bool:
        """**部分成功**：至少一个成功且至少一个失败。

        这是本模块存在的意义——整体失败会丢弃已成功的算力，
        整体成功又是假象；调用方需要显式看到「一半好一半坏」。
        """
        return 0 < len(self.succeeded) < self.total

    @property
    def all_failed(self) -> bool:
        return self.total > 0 and not self.succeeded

    @property
    def total_attempts(self) -> int:
        return sum(o.attempts for o in self.outcomes)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "succeeded": len(self.succeeded),
            "failed": len(self.failed),
            "partial_success": self.partial_success,
            "all_failed": self.all_failed,
            "total_attempts": self.total_attempts,
            "outcomes": [o.to_dict() for o in self.outcomes],
        }


async def run_task(
    spec: TaskSpec,
    runner: TaskRunner,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: Optional[float] = None,
    max_delay: Optional[float] = None,
    jitter: Optional[bool] = None,
    retry_predicate: Callable[[BaseException], bool] = is_retryable,
) -> TaskOutcome:
    """执行单个任务：**局部**重试，耗尽后返回失败结果（不抛异常）。

    与 :func:`agentix.domain.policies.retry.aretry` 的差别：
    装饰器语义是「重试后仍失败就抛」，本函数是**边界**语义
    「把失败转成 :class:`TaskOutcome`」——让批任务可以继续跑完其余分支。

    退避参数（``base_delay`` / ``max_delay`` / ``jitter``）缺省（``None``）时取
    :func:`task_retry_backoff`——即 ``RETRY_BACKOFF_ALIGNED`` 开时与原生
    ``RetryPolicy`` 默认逐项相同的形状，关时是历史实值（LG-1-3）。

    LG-3-1：有 run deadline 时退避停等压进剩余预算（``budgeted_pause``）；
    余量不足以完成这次退避即放弃（不再开下一次尝试），返回的
    :class:`TaskOutcome` 标 ``truncated=True``。无 deadline 时与基线逐位等价。

    ``asyncio.CancelledError`` **一律透传**（取消不是失败，不可被吞掉）。

    P4-4.1（``TOOL_GUARDRAILS_ENABLED``，默认关 ⇒ 下列两处零调用）：

    1. **护栏前置**：每次尝试前查护栏——block（同参已拦）/ halt（工具熔断）
       时**不再启动/继续重试**（重试被护栏拦下的调用是无信息量动作），
       诚实返回 ``skipped_reason="guardrail_*"``；
    2. **终局回喂**：重试耗尽返回失败前，把该工具的**恢复建议**追加进
       ``error``——在 ``retry_predicate`` 判定**之后**拼接，不参与
       可重试性分类（建议文本本身也刻意避开 ASCII 提示词，双保险）。
    """
    shape = task_retry_backoff()
    attempt_backoff = RetryBackoff(
        initial_interval=shape.initial_interval if base_delay is None else base_delay,
        backoff_factor=shape.backoff_factor,
        max_interval=shape.max_interval if max_delay is None else max_delay,
        max_attempts=int(max_attempts),
        jitter=shape.jitter if jitter is None else jitter,
    )
    attempts = 0
    last_error: Optional[BaseException] = None
    truncated_by_budget = False
    started = time.perf_counter()
    # 工具名启发：task 管线的 payload 携带真实工具名（name 是步骤 id）；
    # 无 payload 的通用调用退回 spec.name（护栏对未跟踪工具返回 None，无副作用）。
    guardrail_tool = str((spec.payload or {}).get("tool") or spec.name)

    def _guardrail_skip():
        from agentix.policies.guardrails.guardrails import get_tool_guardrails

        verdict = get_tool_guardrails().check(
            guardrail_tool, (spec.payload or {}).get("args") or {}
        )
        if verdict is None or not verdict.rejected:
            return None
        return TaskOutcome(
            name=spec.name,
            ok=False,
            error=f"guardrail {verdict.tier.value}: {verdict.advice}",
            error_type="GuardrailRejected",
            attempts=attempts,
            retryable=False,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            skipped_reason=f"guardrail_{verdict.tier.value}",
        )

    for attempt in range(max(1, int(max_attempts))):
        if bool(getattr(settings, "TOOL_GUARDRAILS_ENABLED", False)):
            skipped = _guardrail_skip()
            if skipped is not None:
                logger.warning(
                    "task '{}' skipped by guardrail (attempts so far: {})",
                    spec.name, attempts,
                )
                return skipped
        attempts += 1
        try:
            value = await runner(spec)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - 失败转结果，是模块的设计意图
            last_error = exc
            if not retry_predicate(exc) or attempt == max_attempts - 1:
                break
            delay = attempt_backoff.delay(attempt)
            # LG-3-1：有 run deadline 时把停等压进剩余预算；余量不足以完成这次
            # 退避就直接放弃（不再开下一次尝试，标记 truncated）。无 deadline
            # 时 pause == delay、out_of_budget 恒 False，与基线逐位等价。
            pause, out_of_budget = budgeted_pause(delay, remaining_seconds())
            logger.info(
                "task '{}' attempt {}/{} failed ({}), retrying in {:.2f}s",
                spec.name,
                attempts,
                max_attempts,
                type(exc).__name__,
                pause,
            )
            if out_of_budget:
                logger.warning(
                    "task '{}' retry truncated by run budget (needs {:.2f}s, "
                    "{:.2f}s left): giving up after {} attempt(s)",
                    spec.name,
                    delay,
                    pause,
                    attempts,
                )
                truncated_by_budget = True
                break
            # Phase H · W3（RETRY_BUDGET_ENABLED，flag 关 = 原样重试）：
            # task 层是三层（task/tool/retrieval）共享预算的最外层消费点，
            # 耗尽即停（工具/检索层同桶，3×3×3 放大被压成一个总额度）。
            if bool(getattr(settings, "RETRY_BUDGET_ENABLED", False)):
                from agentix.reliability.retry_budget import ensure_retry_budget

                if not ensure_retry_budget().try_consume(layer="task"):
                    logger.warning(
                        "retry budget exhausted for task '{}': giving up",
                        spec.name,
                    )
                    break
            # LG-3-3：退避停等被预算截断时上面已 break，走到这里的重试才会发生。
            record_retry_attempt("task")
            record_retry_sleep("task", pause)
            await asyncio.sleep(pause)
        else:
            return TaskOutcome(
                name=spec.name,
                ok=True,
                value=value,
                attempts=attempts,
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )

    # 不变量：循环至少迭代一次，且成功分支已在循环内 return ⇒ 走到这里必有异常。
    # 用显式异常而非 assert（`python -O` 会剥离 assert，届时会退化成
    # error="NoneType: None" 这种误导性结果）。
    if last_error is None:  # pragma: no cover - 理论不可达
        raise RuntimeError("task_retry: 重试循环既未成功返回也未捕获到异常（不变量被破坏）")
    error_text = f"{type(last_error).__name__}: {last_error}"
    # P4-4.1 终局回喂：可重试性判定已结束，此处追加恢复建议不影响分类；
    # 建议随 TaskOutcome.error 进观测正文，由上层校验/规划通道回喂模型。
    if bool(getattr(settings, "TOOL_GUARDRAILS_ENABLED", False)):
        from agentix.policies.guardrails.guardrails import get_tool_guardrails

        advice = get_tool_guardrails().advice_for(guardrail_tool)
        if advice:
            error_text = f"{error_text}；恢复建议：{advice}"
    last_retryable = bool(retry_predicate(last_error))
    if last_retryable and int(max_attempts) > 1:
        # LG-3-3：可重试错误最终仍失败 = 重试链（含预算/期限截断）耗尽。
        # 不可重试的失败从未进入重试，不落这一账。
        record_retry_exhausted("task")
    return TaskOutcome(
        name=spec.name,
        ok=False,
        error=error_text,
        error_type=type(last_error).__name__,
        attempts=attempts,
        retryable=last_retryable,
        duration_ms=(time.perf_counter() - started) * 1000.0,
        truncated=truncated_by_budget,
    )


async def run_batch(
    specs: Sequence[TaskSpec],
    runner: TaskRunner,
    *,
    max_concurrency: Optional[int] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_predicate: Callable[[BaseException], bool] = is_retryable,
) -> BatchOutcome:
    """并发执行一批任务，**失败隔离**（部分成功语义）。

    :param max_concurrency: 并发上限；``None``/``<=0`` 表示不限制
        （可配合 :func:`agentix.application.services.task_plan.summarize_plan`
        的 ``max_parallelism`` 预分配额度）。
    """
    if not specs:
        return BatchOutcome(outcomes=[])

    sem: Optional[asyncio.Semaphore] = None
    if max_concurrency and int(max_concurrency) > 0:
        sem = asyncio.Semaphore(int(max_concurrency))

    async def guarded(spec: TaskSpec) -> TaskOutcome:
        if sem is None:
            return await run_task(
                spec, runner, max_attempts=max_attempts, retry_predicate=retry_predicate
            )
        async with sem:
            return await run_task(
                spec, runner, max_attempts=max_attempts, retry_predicate=retry_predicate
            )

    outcomes = await asyncio.gather(*(guarded(s) for s in specs))
    batch = BatchOutcome(outcomes=list(outcomes))
    if batch.failed:
        logger.warning(
            "task batch partial result: {}/{} succeeded (attempts={})",
            len(batch.succeeded),
            batch.total,
            batch.total_attempts,
        )
    return batch
