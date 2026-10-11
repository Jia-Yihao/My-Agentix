"""异步重试与错误分类的**向后兼容 re-export 层**。

依据《My Agentix_架构优化方案》P0-2(a) / P1-7：
- ``aretry`` 已上提到 ``agentix.domain.policies.retry``（P0-2(a) 需要把重试接到
  检索传输层，而 application 不得依赖 infrastructure —— 见
  ``tests/test_p17_domain_layer.py`` 的守卫）。
- ``is_retryable`` 早已上提到 ``agentix.domain.policies.error_classification``
  （P1-7），此处仅 re-export。

本模块**只做 re-export**，不再包含本地实现；既有导入路径
``agentix.infrastructure.llm.retry.{aretry,is_retryable}`` 保持可用，
``tests/test_llm_retry.py`` 零改动。
"""

# P1-7 / P0-2(a)：错误分类与重试策略的领域实现（infra 依赖 domain，方向正确）
from agentix.domain.policies.error_classification import (  # noqa: F401
    NON_RETRYABLE_HINTS,
    RETRYABLE_HINTS,
    is_retryable,
)
from agentix.domain.policies.retry import aretry  # noqa: F401

__all__ = ["aretry", "is_retryable", "RETRYABLE_HINTS", "NON_RETRYABLE_HINTS"]
