"""异步重试策略（P0-2(a) 接线：从 infrastructure 上提为领域策略）。

原实现位于 ``agentix/infrastructure/llm/retry.py``。P0-2(a) 需要把 ``aretry``
接到**检索传输层**（``application/agents/.../kb_retrieval.py``），而
application 不得直接依赖 infrastructure 符号（方案 2.2 规则 2/4，且
``tests/test_p17_domain_layer.py`` 有静态守卫）。故把 ``aretry`` 上提到
``domain.policies``：

- ``infrastructure/llm/retry.py`` 从本模块 re-export ``aretry``（infra 依赖
  domain，方向正确），既有导入路径保持不变，``tests/test_llm_retry.py`` 零改动
- ``domain.policies`` 是「判定函数」区（给定异常 → 决策），重试退避策略归属此处

设计依据《My Agentix_架构优化方案》P0-2(a)：
- 刻意不引入 tenacity：项目当前无该依赖，且我们需要「可重试/不可重试」的
  业务级分类，通用库无法表达。
- 错误路径本身不再调用 LLM，只做退避重试，避免「错误处理触发新的模型调用」
  形成死亡螺旋（恢复优先级：重试 → 回滚 → 替代路径 → 熔断 → 人工升级）。

零第三方依赖：仅用 stdlib（``asyncio`` / ``random`` / ``functools`` / ``typing``）。
"""

import asyncio
import random
from dataclasses import dataclass
from functools import wraps
from typing import Any, Awaitable, Callable, TypeVar

from .error_classification import is_retryable

T = TypeVar("T")

#: ``is_retryable`` 从这里转出，供 ``orchestration/langgraph/policies.py`` 接成
#: ``RetryPolicy(retry_on=...)``（LG-1-2）——**方向**：编排层依赖领域层，
#: 领域层不知道 LangGraph 的存在；可重试性的判定只有这一份实现。
__all__ = [
    "aretry",
    "is_retryable",
    "RetryBackoff",
    "RETRY_INITIAL_INTERVAL",
    "RETRY_BACKOFF_FACTOR",
    "RETRY_MAX_INTERVAL",
    "RETRY_MAX_ATTEMPTS",
    "RETRY_JITTER",
]

# ---------------------------------------------------------------------------
# LG-1-3：三层（tool / task / retrieval）自研退避的唯一真源
# ---------------------------------------------------------------------------
# 数值 = LangGraph ``RetryPolicy`` 的默认值（initial_interval=0.5 /
# backoff_factor=2.0 / max_interval=128.0 / max_attempts=3 / jitter=True）。
# 为什么是**抄一份**而不是直接读 RetryPolicy：domain 层零第三方依赖
# （``tests/test_p17_domain_layer.py`` 守着），而 LangGraph 是编排层的依赖。
# 抄本的失真由用例钉住——``tests/test_p150_retry_backoff_alignment.py`` 逐项
# 比对 ``RetryPolicy()`` 的字段，框架改默认值时这里先红。
RETRY_INITIAL_INTERVAL = 0.5
RETRY_BACKOFF_FACTOR = 2.0
RETRY_MAX_INTERVAL = 128.0
RETRY_MAX_ATTEMPTS = 3
RETRY_JITTER = True


@dataclass(frozen=True)
class RetryBackoff:
    """一层重试的退避形状：``delay = min(initial * factor**n, max)``（可选抖动）。

    默认值即 LangGraph 原生默认（见上方常量）。三层的差异只在"用哪一组数"：
    开关关时各层保留历史数值（flag-off 逐位等价），开时统一换成默认构造。
    ``max_attempts`` 是**形状的一部分**，但工具层的实际尝试次数由
    ``ToolSpec.max_retries`` 决定（声明面），这里不参与执行计数。
    """

    initial_interval: float = RETRY_INITIAL_INTERVAL
    backoff_factor: float = RETRY_BACKOFF_FACTOR
    max_interval: float = RETRY_MAX_INTERVAL
    max_attempts: int = RETRY_MAX_ATTEMPTS
    jitter: bool = RETRY_JITTER

    def delay(self, attempt: int) -> float:
        """第 ``attempt`` 次失败后的等待秒数（``attempt`` 从 0 起）。"""
        delay = min(
            self.initial_interval * (self.backoff_factor**attempt),
            self.max_interval,
        )
        if self.jitter:
            delay *= 0.5 + random.random() / 2
        return delay


def aretry(
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_INITIAL_INTERVAL,
    max_delay: float = RETRY_MAX_INTERVAL,
    jitter: bool = RETRY_JITTER,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """指数退避 + 抖动的异步重试装饰器。

    关键设计（依据方案 P0-2 / [第5章]）：错误路径本身不再调用 LLM，
    只做退避重试，避免「错误处理触发新的模型调用」形成死亡螺旋。

    可重试性由 ``is_retryable`` 判定（瞬时网络/限流/5xx → 重试；
    鉴权/参数错误 → 立即抛出）。``asyncio.CancelledError`` 一律透传，
    不可被重试逻辑吞掉。
    """

    def deco(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        shape = RetryBackoff(
            initial_interval=base_delay,
            max_interval=max_delay,
            max_attempts=max_attempts,
            jitter=jitter,
        )

        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            last: BaseException | None = None
            for attempt in range(max_attempts):
                try:
                    return await fn(*args, **kwargs)
                except asyncio.CancelledError:
                    raise  # 取消信号必须透传，不可被重试吞掉
                except BaseException as e:  # noqa: BLE001
                    last = e
                    if not is_retryable(e) or attempt == max_attempts - 1:
                        raise
                    await asyncio.sleep(shape.delay(attempt))
            raise last  # type: ignore[misc]

        return wrapper

    return deco
