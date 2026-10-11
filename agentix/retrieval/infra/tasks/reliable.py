"""可复用的同步任务重试策略。

任务入口（Celery 和 FastAPI BackgroundTasks）都通过这里执行，避免部署方式
改变任务的重试语义。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 30.0
    fatal_exceptions: tuple[type[BaseException], ...] = (ValueError,)


@dataclass(frozen=True)
class TaskResult:
    ok: bool
    attempts: int
    error: BaseException | None = None
    dead_letter: bool = False


def run_with_retry(
    operation: Callable[[], None],
    policy: RetryPolicy | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> TaskResult:
    """执行操作并按指数退避重试，返回结果而不是泄露异常细节。"""
    retry_policy = policy or RetryPolicy()
    if retry_policy.max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    for attempt in range(1, retry_policy.max_attempts + 1):
        try:
            operation()
            return TaskResult(ok=True, attempts=attempt)
        except retry_policy.fatal_exceptions as exc:
            return TaskResult(ok=False, attempts=attempt, error=exc, dead_letter=True)
        except Exception as exc:  # noqa: BLE001
            if attempt == retry_policy.max_attempts:
                return TaskResult(ok=False, attempts=attempt, error=exc, dead_letter=True)
            delay = min(
                retry_policy.base_delay_seconds * (2 ** (attempt - 1)),
                retry_policy.max_delay_seconds,
            )
            sleep(delay)

    raise AssertionError("retry loop must return a TaskResult")