"""领域策略（policies）——错误分类与降级决策。

依据方案 2.2 节：``policies/`` 承载「错误分类·降级策略」。

分区约定：
- ``domain/errors.py``：**词汇表**（``ErrorCode`` / ``AgentError`` /
  ``DEGRADABLE`` / ``HTTP_FATAL``）——「有哪些错误、各属哪一类」
- ``domain/policies/``：**判定函数**（给定异常/错误码 → 决策）——「遇到它该怎么办」

本包零第三方依赖。
"""

from .error_classification import (
    NON_RETRYABLE_HINTS,
    RETRYABLE_HINTS,
    is_retryable,
)
from .retry import (
    RETRY_BACKOFF_FACTOR,
    RETRY_INITIAL_INTERVAL,
    RETRY_JITTER,
    RETRY_MAX_ATTEMPTS,
    RETRY_MAX_INTERVAL,
    RetryBackoff,
    aretry,
    budgeted_pause,
    is_retry_truncated,
    remaining_seconds,
    set_remaining_provider,
)

__all__ = [
    "NON_RETRYABLE_HINTS",
    "RETRYABLE_HINTS",
    "is_retryable",
    "aretry",
    "RetryBackoff",
    "RETRY_INITIAL_INTERVAL",
    "RETRY_BACKOFF_FACTOR",
    "RETRY_MAX_INTERVAL",
    "RETRY_MAX_ATTEMPTS",
    "RETRY_JITTER",
    "budgeted_pause",
    "remaining_seconds",
    "set_remaining_provider",
    "is_retry_truncated",
]
