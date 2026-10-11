"""
可靠性模块

提供限流器和熔断器，保障系统在高并发下的稳定性。

使用方式：
    from agentix.reliability import (
        RateLimiter,
        RedisRateLimiter,
        CircuitBreaker,
        circuit_breaker,
    )
"""

from agentix.reliability.rate_limiter import (
    RateLimiter,
    RedisRateLimiter,
    SlidingWindowRateLimiter,
    RateLimitExceeded,
    create_rate_limit_dependency,
)

from agentix.reliability.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
    CircuitBreakerOpenError,
    CircuitBreakerHalfOpenError,
    CircuitBreakerStats,
    CircuitBreakerRegistry,
    circuit_breaker,
    registry,
)

__all__ = [
    # 限流器
    "RateLimiter",
    "RedisRateLimiter",
    "SlidingWindowRateLimiter",
    "RateLimitExceeded",
    "create_rate_limit_dependency",
    # 熔断器
    "CircuitBreaker",
    "CircuitState",
    "CircuitBreakerOpenError",
    "CircuitBreakerHalfOpenError",
    "CircuitBreakerStats",
    "CircuitBreakerRegistry",
    "circuit_breaker",
    "registry",
]
