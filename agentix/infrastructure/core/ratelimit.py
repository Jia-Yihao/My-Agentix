"""
Rate limiting and circuit breaker for Agent endpoints.

Rate Limiter:
- Token bucket algorithm backed by Redis
- Per-user and per-endpoint rate limits
- Configurable burst and refill rate

Circuit Breaker:
- Three states: CLOSED (normal), OPEN (failing), HALF_OPEN (testing)
- Auto-trips after consecutive failures
- Auto-recovers after cooldown period

Usage as FastAPI middleware or dependency.
"""

import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Tuple

from fastapi import HTTPException, Request, status
from loguru import logger
from redis.asyncio import Redis

from agentix.config import settings


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class RateLimitConfig:
    """Configuration for token bucket rate limiter."""
    max_tokens: int = 60
    refill_rate: float = 1.0
    refill_interval: float = 1.0
    burst_multiplier: float = 1.5


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker."""
    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    half_open_max_calls: int = 3


class TokenBucketRateLimiter:
    """Redis-backed token bucket rate limiter.

    Each key gets a bucket with `max_tokens` capacity.
    Tokens refill at `refill_rate` per `refill_interval` seconds.
    """

    def __init__(
        self,
        *,
        redis_client: Optional[Redis] = None,
        redis_url: Optional[str] = None,
        config: Optional[RateLimitConfig] = None,
        prefix: str = "ratelimit",
    ):
        self._redis = redis_client or Redis.from_url(
            redis_url or settings.REDIS_URL,
            decode_responses=True,
        )
        self.config = config or RateLimitConfig()
        self.prefix = prefix

    async def acquire(self, key: str, tokens: int = 1) -> bool:
        """Try to acquire tokens from the bucket.

        Returns True if tokens were acquired, False if rate limited.
        """
        bucket_key = f"{self.prefix}:{key}"
        now = time.time()

        pipe = self._redis.pipeline()
        pipe.hgetall(bucket_key)
        bucket_data = (await pipe.execute())[0]

        if not bucket_data:
            current_tokens = float(self.config.max_tokens)
            last_refill = now
        else:
            current_tokens = float(bucket_data.get("tokens", self.config.max_tokens))
            last_refill = float(bucket_data.get("last_refill", now))

        elapsed = now - last_refill
        refill_amount = (elapsed / self.config.refill_interval) * self.config.refill_rate
        current_tokens = min(
            self.config.max_tokens * self.config.burst_multiplier,
            current_tokens + refill_amount,
        )

        if current_tokens < tokens:
            logger.warning("Rate limit exceeded: key={} tokens={:.1f}", key, current_tokens)
            return False

        current_tokens -= tokens

        pipe = self._redis.pipeline()
        pipe.hset(bucket_key, mapping={
            "tokens": str(current_tokens),
            "last_refill": str(now),
        })
        pipe.expire(bucket_key, 3600)
        await pipe.execute()

        return True

    async def get_remaining(self, key: str) -> float:
        """Get remaining tokens for a key."""
        bucket_key = f"{self.prefix}:{key}"
        data = await self._redis.hgetall(bucket_key)
        if not data:
            return float(self.config.max_tokens)

        current_tokens = float(data.get("tokens", self.config.max_tokens))
        last_refill = float(data.get("last_refill", time.time()))

        elapsed = time.time() - last_refill
        refill_amount = (elapsed / self.config.refill_interval) * self.config.refill_rate
        return min(
            self.config.max_tokens * self.config.burst_multiplier,
            current_tokens + refill_amount,
        )

    async def reset(self, key: str) -> None:
        """Reset a rate limit bucket."""
        await self._redis.delete(f"{self.prefix}:{key}")


class CircuitBreaker:
    """Circuit breaker for external service calls.

    States:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Service is failing, requests are rejected immediately
    - HALF_OPEN: Testing if service has recovered
    """

    def __init__(
        self,
        name: str,
        *,
        redis_client: Optional[Redis] = None,
        redis_url: Optional[str] = None,
        config: Optional[CircuitBreakerConfig] = None,
    ):
        self.name = name
        self._redis = redis_client or Redis.from_url(
            redis_url or settings.REDIS_URL,
            decode_responses=True,
        )
        self.config = config or CircuitBreakerConfig()
        self._key = f"circuit:{name}"

    async def get_state(self) -> CircuitState:
        """Get current circuit state."""
        state = await self._redis.hget(self._key, "state")
        if not state:
            return CircuitState.CLOSED
        return CircuitState(state)

    async def call(self, func, *args, **kwargs):
        """Execute a function through the circuit breaker.

        Raises HTTPException(503) if circuit is open.
        """
        state = await self.get_state()

        if state == CircuitState.OPEN:
            opened_at = float(await self._redis.hget(self._key, "opened_at") or 0)
            if time.time() - opened_at >= self.config.recovery_timeout:
                await self._set_state(CircuitState.HALF_OPEN)
                logger.info("Circuit breaker {} entering HALF_OPEN", self.name)
            else:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Service '{self.name}' is temporarily unavailable (circuit open)",
                )

        try:
            result = await func(*args, **kwargs)
            await self._on_success()
            return result
        except Exception:
            await self._on_failure()
            raise

    async def _on_success(self) -> None:
        """Handle successful call."""
        state = await self.get_state()
        if state == CircuitState.HALF_OPEN:
            await self._set_state(CircuitState.CLOSED)
            logger.info("Circuit breaker {} recovered to CLOSED", self.name)
        await self._redis.hset(self._key, "consecutive_failures", "0")

    async def _on_failure(self) -> None:
        """Handle failed call."""
        failures = int(await self._redis.hget(self._key, "consecutive_failures") or 0) + 1
        await self._redis.hset(self._key, "consecutive_failures", str(failures))

        if failures >= self.config.failure_threshold:
            await self._set_state(CircuitState.OPEN)
            logger.warning(
                "Circuit breaker {} tripped: {} consecutive failures",
                self.name,
                failures,
            )

    async def _set_state(self, state: CircuitState) -> None:
        """Set circuit state."""
        pipe = self._redis.pipeline()
        pipe.hset(self._key, "state", state.value)
        if state == CircuitState.OPEN:
            pipe.hset(self._key, "opened_at", str(time.time()))
        pipe.expire(self._key, 3600)
        await pipe.execute()

    async def reset(self) -> None:
        """Reset circuit breaker to closed state."""
        await self._redis.delete(self._key)


_rate_limiter_instance: Optional[TokenBucketRateLimiter] = None

# 审计批次 A3（2026-09-28）：rate_limit_dependency 的按参数实例缓存——
# 不同 (max_requests, window_seconds) 各用独立实例，参数天然隔离。
_param_limiter_cache: Dict[Tuple[int, int], TokenBucketRateLimiter] = {}


def get_rate_limiter() -> TokenBucketRateLimiter:
    """Get or create the global rate limiter instance."""
    global _rate_limiter_instance
    if _rate_limiter_instance is None:
        _rate_limiter_instance = TokenBucketRateLimiter()
    return _rate_limiter_instance


def _limiter_for(max_requests: int, window_seconds: int) -> TokenBucketRateLimiter:
    """按参数派生限流器（审计批次 A3）。

    原实现把参数写进**共享** ``limiter.config``——并发下不同端点/参数会
    互相踩掉阈值（天窗型竞态），且热改参数会污染全局。现按参数缓存
    独立实例：同参数复用、不同参数隔离，构造仅在 ``RATE_LIMIT_ENABLED``
    打开后发生（关闭时零开销，见 ``rate_limit_dependency``）。
    """
    cache_key = (max_requests, window_seconds)
    limiter = _param_limiter_cache.get(cache_key)
    if limiter is None:
        limiter = TokenBucketRateLimiter(
            config=RateLimitConfig(
                max_tokens=max_requests,
                refill_rate=max_requests / window_seconds,
            )
        )
        _param_limiter_cache[cache_key] = limiter
    return limiter


async def rate_limit_dependency(
    request: Request,
    max_requests: int = 30,
    window_seconds: int = 60,
) -> None:
    """FastAPI dependency for rate limiting endpoints.

    Usage:
        @router.post("/chat")
        async def chat(request: ChatRequest, _: None = Depends(rate_limit_dependency)):
            ...

    开关：由 ``RATE_LIMIT_ENABLED`` 单独控制。

    为什么**不**用 ``REQUIRE_API_KEY`` 作开关：这两件事在语义上无关，共用一把闸门会
    制造一个静默的陷阱——"我把鉴权关了"（本地调试常见）会连带把限流也关掉，且没有任何
    提示。反过来，生产环境要求 ``REQUIRE_API_KEY=true`` 也不应该顺带决定限流是否生效。
    限流该不该开，只看 ``RATE_LIMIT_ENABLED``。

    参数口径（审计批次 A3）：不写共享 ``limiter.config``（原实现并发互踩），
    改走 ``_limiter_for`` 按参数派生——不同调用点的阈值互不干扰。

    注意：真正的生产限流入口是 ``GlobalRateLimitMiddleware``（纯 ASGI 中间件，挂在
    最外层），以及检索域 chat 路由自己的 ``_chat_rate_limit``；本函数是一个未被调用的
    通用 dependency，保留它是因为它被作为对外可用的集成点，但**不要**再把它接到路由上
    而不检查开关语义。
    """
    if not settings.RATE_LIMIT_ENABLED:
        return

    client_ip = request.client.host if request.client else "unknown"
    key = f"endpoint:{request.url.path}:{client_ip}"

    # 审计批次 A3：per-params 派生（不再改写共享 config）
    limiter = _limiter_for(max_requests, window_seconds)

    allowed = await limiter.acquire(key)
    if not allowed:
        remaining = await limiter.get_remaining(key)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded. Try again in {window_seconds} seconds.",
            headers={
                "X-RateLimit-Limit": str(max_requests),
                "X-RateLimit-Remaining": str(int(remaining)),
                "Retry-After": str(window_seconds),
            },
        )
