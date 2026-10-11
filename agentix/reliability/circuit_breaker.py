"""
可靠性 - 熔断器（Circuit Breaker）

防止级联故障，当下游服务不可用时自动熔断，避免雪崩。

状态机：
    CLOSED（正常）→ 失败次数达到阈值 → OPEN（熔断）
    OPEN → 超时后 → HALF_OPEN（半开）
    HALF_OPEN → 探测成功 → CLOSED
    HALF_OPEN → 探测失败 → OPEN

使用方式：
    from agentix.reliability.circuit_breaker import CircuitBreaker
    
    # 创建熔断器
    breaker = CircuitBreaker(
        name="llm_api",
        failure_threshold=5,
        recovery_timeout=60,
        half_open_max_calls=3,
    )
    
    # 使用
    try:
        result = await breaker.call(llm_api_call, query, context)
    except CircuitBreakerOpenError:
        # 熔断中，返回降级结果
        result = fallback_response()
    
    # 装饰器方式
    @circuit_breaker(name="embedding_api", failure_threshold=3)
    async def call_embedding(texts: list[str]) -> list[list[float]]:
        ...
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass
from functools import wraps
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class CircuitState(enum.Enum):
    """熔断器状态"""
    CLOSED = "closed"        # 正常，允许请求
    OPEN = "open"            # 熔断，拒绝请求
    HALF_OPEN = "half_open"  # 半开，允许少量探测请求


class CircuitBreakerOpenError(Exception):
    """熔断器打开异常"""
    def __init__(self, name: str, remaining_timeout: float = 0):
        self.name = name
        self.remaining_timeout = remaining_timeout
        super().__init__(
            f"Circuit breaker '{name}' is OPEN, "
            f"retry after {remaining_timeout:.1f}s"
        )


class CircuitBreakerHalfOpenError(Exception):
    """熔断器半开状态，探测请求数已满"""
    def __init__(self, name: str):
        self.name = name
        super().__init__(f"Circuit breaker '{name}' is HALF_OPEN, max probe calls reached")


@dataclass
class CircuitBreakerStats:
    """熔断器统计信息"""
    name: str
    state: CircuitState
    failure_count: int
    success_count: int
    total_calls: int
    last_failure_time: Optional[float]
    last_state_change: float
    failure_threshold: int
    recovery_timeout: float


class CircuitBreaker:
    """熔断器
    
    Args:
        name: 熔断器名称（用于日志和指标）
        failure_threshold: 连续失败次数阈值，达到后熔断
        recovery_timeout: 熔断后等待时间（秒），超时后进入半开状态
        half_open_max_calls: 半开状态允许的最大探测请求数
        success_threshold: 半开状态连续成功次数，达到后恢复关闭状态
        on_state_change: 状态变更回调
    """
    
    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        half_open_max_calls: int = 3,
        success_threshold: int = 2,
        on_state_change: Optional[Callable] = None,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        self.success_threshold = success_threshold
        self.on_state_change = on_state_change
        
        # 状态
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._half_open_calls = 0
        self._total_calls = 0
        self._last_failure_time: Optional[float] = None
        self._last_state_change = time.monotonic()
        self._lock = asyncio.Lock()
    
    @property
    def state(self) -> CircuitState:
        """当前状态（自动检查是否需要从 OPEN 转为 HALF_OPEN）"""
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_state_change
            if elapsed >= self.recovery_timeout:
                self._transition_to(CircuitState.HALF_OPEN)
        return self._state
    
    @property
    def stats(self) -> CircuitBreakerStats:
        """获取统计信息"""
        return CircuitBreakerStats(
            name=self.name,
            state=self.state,
            failure_count=self._failure_count,
            success_count=self._success_count,
            total_calls=self._total_calls,
            last_failure_time=self._last_failure_time,
            last_state_change=self._last_state_change,
            failure_threshold=self.failure_threshold,
            recovery_timeout=self.recovery_timeout,
        )
    
    def _transition_to(self, new_state: CircuitState):
        """状态转换"""
        old_state = self._state
        self._state = new_state
        self._last_state_change = time.monotonic()
        
        if new_state == CircuitState.HALF_OPEN:
            self._half_open_calls = 0
        elif new_state == CircuitState.CLOSED:
            self._failure_count = 0
            self._success_count = 0
        
        logger.info(
            "Circuit breaker '%s': %s → %s",
            self.name, old_state.value, new_state.value,
        )

        # 企业化 P1-2.2：熔断状态可观测（Gauge + 转换事件计数）。
        # 指标上报失败绝不影响熔断语义本身。
        try:
            from agentix.observability.metrics import record_circuit_transition

            record_circuit_transition(self.name, old_state.value, new_state.value)
        except Exception as e:  # noqa: BLE001
            logger.warning("Circuit metrics export failed: %s", e)

        if self.on_state_change:
            try:
                self.on_state_change(self.name, old_state, new_state)
            except Exception as e:
                logger.warning("State change callback error: %s", e)
    
    async def call(self, func: Callable, *args, **kwargs) -> Any:
        """通过熔断器调用函数
        
        Args:
            func: 要调用的异步函数
            *args, **kwargs: 函数参数
            
        Returns:
            函数返回值
            
        Raises:
            CircuitBreakerOpenError: 熔断器打开时
            CircuitBreakerHalfOpenError: 半开状态探测请求数已满时
        """
        async with self._lock:
            current_state = self.state
            
            # OPEN 状态：拒绝请求
            if current_state == CircuitState.OPEN:
                elapsed = time.monotonic() - self._last_state_change
                remaining = self.recovery_timeout - elapsed
                raise CircuitBreakerOpenError(self.name, max(0, remaining))
            
            # HALF_OPEN 状态：限制探测请求数
            if current_state == CircuitState.HALF_OPEN:
                if self._half_open_calls >= self.half_open_max_calls:
                    raise CircuitBreakerHalfOpenError(self.name)
                self._half_open_calls += 1
            
            self._total_calls += 1
        
        # 执行请求（不在锁内，避免阻塞）
        try:
            result = await func(*args, **kwargs)
            await self._on_success()
            return result
        except Exception as e:
            await self._on_failure(e)
            raise
    
    async def _on_success(self):
        """请求成功时的处理"""
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    self._transition_to(CircuitState.CLOSED)
            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0
    
    async def _on_failure(self, error: Exception):
        """请求失败时的处理"""
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            
            if self._state == CircuitState.HALF_OPEN:
                self._transition_to(CircuitState.OPEN)
            elif self._state == CircuitState.CLOSED:
                if self._failure_count >= self.failure_threshold:
                    self._transition_to(CircuitState.OPEN)
    
    def reset(self):
        """重置熔断器"""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._half_open_calls = 0
        self._last_state_change = time.monotonic()
        logger.info("Circuit breaker '%s' reset", self.name)
    
    def allow_request(self) -> bool:
        """检查是否允许请求（同步版本，用于快速检查）"""
        current_state = self.state
        if current_state == CircuitState.CLOSED:
            return True
        if current_state == CircuitState.HALF_OPEN:
            return self._half_open_calls < self.half_open_max_calls
        return False


# ============================================================
# 装饰器
# ============================================================

def circuit_breaker(
    name: str = "default",
    failure_threshold: int = 5,
    recovery_timeout: float = 60.0,
    half_open_max_calls: int = 3,
    fallback: Optional[Callable] = None,
):
    """熔断器装饰器
    
    Args:
        name: 熔断器名称
        failure_threshold: 失败阈值
        recovery_timeout: 恢复超时
        half_open_max_calls: 半开最大调用数
        fallback: 降级函数（熔断时调用）
    
    用法：
        @circuit_breaker(name="llm", failure_threshold=3, fallback=fallback_llm)
        async def call_llm(query: str) -> str:
            ...
    """
    breaker = CircuitBreaker(
        name=name,
        failure_threshold=failure_threshold,
        recovery_timeout=recovery_timeout,
        half_open_max_calls=half_open_max_calls,
    )
    
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await breaker.call(func, *args, **kwargs)
            except (CircuitBreakerOpenError, CircuitBreakerHalfOpenError):
                if fallback:
                    return await fallback(*args, **kwargs)
                raise
        
        wrapper.breaker = breaker
        return wrapper
    
    return decorator


# ============================================================
# 熔断器注册表
# ============================================================

class CircuitBreakerRegistry:
    """熔断器注册表，管理所有熔断器实例"""
    
    def __init__(self):
        self._breakers: dict[str, CircuitBreaker] = {}
    
    def get_or_create(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        half_open_max_calls: int = 3,
    ) -> CircuitBreaker:
        """获取或创建熔断器。

        语义：**首建者胜（first-wins）**。同名熔断器一旦创建，后续调用传入的参数
        会被忽略——这是刻意的（同一依赖的熔断状态必须全局唯一，不能因为第二个调用点
        传了不同阈值就换一个实例）。但"静默忽略"是危险的：调用方会以为自己配置的
        阈值生效了，实际没有。因此这里在**参数不一致时发一条 warning**，把
        "谁的配置赢了"讲清楚。

        为什么不 raise：同名不同参在生产里可能就是两个调用点各写了各的默认值，
        属于"配置漂移"而非"逻辑错误"；直接抛异常会把一个可观测问题升级成可用性事故。
        """
        existing = self._breakers.get(name)
        if existing is not None:
            mismatches = []
            if existing.failure_threshold != failure_threshold:
                mismatches.append(
                    f"failure_threshold: 生效 {existing.failure_threshold} != 本次传入 {failure_threshold}"
                )
            if float(existing.recovery_timeout) != float(recovery_timeout):
                mismatches.append(
                    f"recovery_timeout: 生效 {existing.recovery_timeout} != 本次传入 {recovery_timeout}"
                )
            if existing.half_open_max_calls != half_open_max_calls:
                mismatches.append(
                    f"half_open_max_calls: 生效 {existing.half_open_max_calls} != 本次传入 {half_open_max_calls}"
                )
            if mismatches:
                # 注意：本模块用的是 stdlib logging（%s 风格），不是 loguru 的 {} 风格。
                logger.warning(
                    "circuit breaker %r already exists; keeping the FIRST-created config "
                    "and ignoring this call's params: %s",
                    name,
                    "; ".join(mismatches),
                )
            return existing

        self._breakers[name] = CircuitBreaker(
            name=name,
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
            half_open_max_calls=half_open_max_calls,
        )
        return self._breakers[name]
    
    def get(self, name: str) -> Optional[CircuitBreaker]:
        """获取熔断器"""
        return self._breakers.get(name)
    
    def get_all_stats(self) -> list[CircuitBreakerStats]:
        """获取所有熔断器的统计信息"""
        return [breaker.stats for breaker in self._breakers.values()]
    
    def reset_all(self):
        """重置所有熔断器"""
        for breaker in self._breakers.values():
            breaker.reset()


# 全局注册表
registry = CircuitBreakerRegistry()
