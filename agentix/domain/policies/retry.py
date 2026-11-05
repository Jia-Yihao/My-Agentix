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
from typing import Any, Awaitable, Callable, Optional, Tuple, TypeVar

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
    "budgeted_pause",
    "remaining_seconds",
    "set_remaining_provider",
    "RETRY_TRUNCATED_ATTR",
    "mark_retry_truncated",
    "is_retry_truncated",
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


# ---------------------------------------------------------------------------
# LG-3-1：退避停等的预算感知（deadline 的存储规范位在 domain 的请求作用域，注册由 application 在导入期做，读取在 domain）
# ---------------------------------------------------------------------------
# 为什么仍是「注入读取函数」而不是在这里直接读作用域：M2-2 拆边后 ``RequestScope``
# （含 deadline 的**存储**）的规范位已下沉 ``agentix/domain/request_scope.py``，
# 但**写入与注册**（``set_remaining_provider(...)``）仍由 application 在导入期完成——
# 「当前作用域从哪来」由写入方决定；本层只持有一个读取函数，退避算术只有一份
# （约定 6 ④：同一个判定不许两个源）。
_remaining_provider: Optional[Callable[[], Optional[float]]] = None

#: 「因 run 预算耗尽而放弃重试」打在最后抛出的异常上的属性名。
RETRY_TRUNCATED_ATTR = "retry_truncated"


def set_remaining_provider(
    provider: Optional[Callable[[], Optional[float]]],
) -> None:
    """注册「距 deadline 的剩余秒数」读取函数（application 在导入期注册）。"""
    global _remaining_provider
    _remaining_provider = provider


def remaining_seconds() -> Optional[float]:
    """当前 run 的剩余预算秒数；无 provider / 无 deadline / 读取故障 → ``None``。

    ``None`` 是**回原行为**的信号：无 deadline 时退避与基线逐位等价。
    """
    provider = _remaining_provider
    if provider is None:
        return None
    try:
        value = provider()
    except Exception:  # noqa: BLE001 - 预算读取故障不得拖垮重试主链路
        return None
    if value is None:
        return None
    return max(0.0, float(value))


def budgeted_pause(delay: float, remaining: Optional[float]) -> Tuple[float, bool]:
    """把一次退避停等压进剩余预算：返回 ``(实际停等秒数, 是否应放弃重试)``。

    - ``remaining is None``（无 deadline）⇒ ``(delay, False)``，与基线逐位等价；
    - ``remaining > delay`` ⇒ ``(delay, False)``，预算够，原样停等；
    - ``remaining <= delay`` ⇒ ``(remaining, True)``：只等剩余预算即放弃——停满
      之后下一次尝试必然越过 deadline，重试不会得到结果，只会把「超时」拖到
      最后一次尝试之后才暴露。
    """
    if remaining is None or remaining > delay:
        return delay, False
    return max(0.0, remaining), True


def mark_retry_truncated(exc: BaseException) -> BaseException:
    """给「因预算耗尽而放弃重试」的异常打标（调用方用 :func:`is_retry_truncated` 读）。

    只加属性、不改类型与消息：上层的错误分类（``is_retryable``）不受影响。
    """
    try:
        setattr(exc, RETRY_TRUNCATED_ATTR, True)
    except Exception:  # noqa: BLE001 - 标记失败不得影响异常继续抛出
        pass
    return exc


def is_retry_truncated(exc: BaseException) -> bool:
    """该异常是否由「重试因 run 预算耗尽被截断」产生（未打标 ⇒ False）。"""
    return bool(getattr(exc, RETRY_TRUNCATED_ATTR, False))


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

    LG-3-1：有 run deadline 时，退避停等压进剩余预算（``budgeted_pause``）；
    余量不足以完成这次退避即放弃，并在抛出的异常上打 ``retry_truncated`` 标。
    无 deadline（``remaining_seconds() is None``）时的代码路径与基线逐位相同。
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
                    pause, out_of_budget = budgeted_pause(
                        shape.delay(attempt), remaining_seconds()
                    )
                    if out_of_budget:
                        # 余量不足以完成这次退避：不再开下一次尝试，原样抛出
                        # （只打标，不改类型/消息）。
                        mark_retry_truncated(e)
                        raise
                    await asyncio.sleep(pause)
            raise last  # type: ignore[misc]

        return wrapper

    return deco
