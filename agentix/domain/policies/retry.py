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
from functools import wraps
from typing import Any, Awaitable, Callable, TypeVar

from .error_classification import is_retryable

T = TypeVar("T")

__all__ = ["aretry"]


def aretry(
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    jitter: bool = True,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """指数退避 + 抖动的异步重试装饰器。

    关键设计（依据方案 P0-2 / [第5章]）：错误路径本身不再调用 LLM，
    只做退避重试，避免「错误处理触发新的模型调用」形成死亡螺旋。

    可重试性由 ``is_retryable`` 判定（瞬时网络/限流/5xx → 重试；
    鉴权/参数错误 → 立即抛出）。``asyncio.CancelledError`` 一律透传，
    不可被重试逻辑吞掉。
    """

    def deco(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
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
                    delay = min(base_delay * (2**attempt), max_delay)
                    if jitter:
                        delay *= 0.5 + random.random() / 2
                    await asyncio.sleep(delay)
            raise last  # type: ignore[misc]

        return wrapper

    return deco
