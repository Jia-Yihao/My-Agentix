"""run 级共享重试预算桶（防 3×3×3 重试放大）。

三层叠加的最坏路径 = 任务层(3) × 工具层(3) × LLM SDK(3~4) ≈ 27~36 次
调用/单位，且只有最外层做了可重试性分类。本模块提供：

1. :class:`RetryBudget`——一次 run 共享的 token 桶，三层重试前
   ``try_consume(layer=...)`` 抢占，耗尽即停（各层首attempt不受影响）；
2. 分类审计口径——工具层/检索层在 flag 开启时补
   ``domain.policies.error_classification.is_retryable`` 闸
   （仅 timeout/429/503/reset 等瞬时错误可重试；鉴权/参数错误不重试）。

**作用域**：优先挂 ``RequestScope``（chat 主链路，跨 gather 子任务共享同一
对象引用）；无 scope 时回落本模块 ContextVar（同一 asyncio 任务内跨层共享，
任务后台各自独立桶——比每层各 3 次仍严格更紧）。``RETRY_BUDGET_ENABLED=false``
（默认）时 ``ensure_retry_budget`` 永不被调用方触发，各层现行为逐位等价。
"""

from __future__ import annotations

import contextvars
from collections import Counter
from typing import Dict, Optional

from agentix.config import settings
from agentix.domain.policies.error_classification import is_retryable

__all__ = [
    "RetryBudget",
    "retryable_error",
    "current_retry_budget",
    "ensure_retry_budget",
    "reset_retry_budget",
]

#: 无 RequestScope 时的任务级回落桶（asyncio task context 内跨层共享）。
_fallback_var: contextvars.ContextVar[Optional["RetryBudget"]] = contextvars.ContextVar(
    "retry_budget_fallback", default=None
)


def retryable_error(exc: BaseException) -> bool:
    """审计口径：仅瞬时性错误可重试（timeout/429/503/reset 等）。

    包装 ``is_retryable``——其判定串含异常类名（``TimeoutError`` →
    "timeout" 命中），鉴权/参数类 4xx 先否后肯。
    """
    return is_retryable(exc)


class RetryBudget:
    """run 内共享的重试 token 桶（同步操作：三层皆在事件循环内消费）。"""

    def __init__(self, tokens: int) -> None:
        self._remaining = max(0, int(tokens))
        self._consumed = 0
        self._by_layer: Counter[str] = Counter()

    @property
    def remaining(self) -> int:
        return self._remaining

    @property
    def consumed(self) -> int:
        return self._consumed

    def try_consume(self, *, layer: str) -> bool:
        """消费一枚重试 token（``False`` = 预算耗尽，调用方停止重试）。"""
        if self._remaining <= 0:
            return False
        self._remaining -= 1
        self._consumed += 1
        self._by_layer[layer] += 1
        return True

    def stats(self) -> Dict[str, int]:
        return {
            "remaining": self._remaining,
            "consumed": self._consumed,
            **{f"layer_{k}": v for k, v in self._by_layer.items()},
        }


def current_retry_budget() -> Optional[RetryBudget]:
    """当前共享预算（无则 None）；永不创建。"""
    from agentix.application.services.request_scope import get_current_scope

    scope = get_current_scope()
    if scope is not None and scope.retry_budget is not None:
        return scope.retry_budget  # type: ignore[return-value]
    return _fallback_var.get()


def ensure_retry_budget() -> RetryBudget:
    """取或建共享预算（懒建：首个重试点触发，flag 关时不会被调用）。"""
    existing = current_retry_budget()
    if existing is not None:
        return existing
    budget = RetryBudget(int(getattr(settings, "RETRY_BUDGET_TOKENS", 6) or 6))
    from agentix.application.services.request_scope import get_current_scope

    scope = get_current_scope()
    if scope is not None:
        scope.retry_budget = budget
    else:
        _fallback_var.set(budget)
    return budget


def reset_retry_budget() -> None:
    """测试隔离：清空回落桶（RequestScope 随请求生命周期自清）。"""
    _fallback_var.set(None)
