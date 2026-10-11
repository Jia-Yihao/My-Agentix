"""瞬时性错误判定策略（P1-7：从 infrastructure 上提为领域策略）。

原实现位于 ``agentix/infrastructure/llm/retry.py``，但调用方
``application/services/chat_service.py`` 需要它做「503 上游不可用 vs 500 内部
错误」的业务分类——**application 不应依赖 infrastructure 符号**（方案 2.2
规则 2），故上提到 ``domain.policies``。

上提后：
- ``infrastructure/llm/retry.py`` 从本模块 import（infra 依赖 domain，方向正确）
- 既有导入路径 ``agentix.infrastructure.llm.retry.is_retryable`` **保持不变**
  （向后兼容 re-export），测试零改动

零第三方依赖：仅用 stdlib。
"""

__all__ = ["NON_RETRYABLE_HINTS", "RETRYABLE_HINTS", "is_retryable"]

# 可重试 / 瞬时的失败特征：网络、限流、服务端错误
RETRYABLE_HINTS = (
    "timeout",
    "timed out",
    "rate limit",
    "429",
    "500",
    "502",
    "503",
    "504",
    "connection",
    "temporarily",
    "overloaded",
)

# 不可重试：鉴权/权限/参数错误——重试无意义且会放大故障
NON_RETRYABLE_HINTS = (
    "authentication",
    "invalid api key",
    "401",
    "403",
    "invalid_request",
    "400",
)


def is_retryable(exc: BaseException) -> bool:
    """判断异常是否值得重试（也用于区分「上游瞬断」与「内部缺陷」）。

    鉴权失败 / 参数错误 → False（重试只会重复失败）；
    网络 / 限流 / 服务端错误 → True。

    判定顺序（有意）：**先**匹配不可重试特征再匹配可重试特征——因为
    "invalid api key" 之类文本里可能同时含 "500" 等数字，先否后肯避免误判。
    """
    msg = f"{type(exc).__name__}: {exc}".lower()
    if any(h in msg for h in NON_RETRYABLE_HINTS):
        return False
    return any(h in msg for h in RETRYABLE_HINTS)
