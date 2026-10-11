"""
可靠性 - 令牌桶限流器

支持单机内存限流和 Redis 分布式限流两种模式。

使用方式：
    from agentix.reliability.rate_limiter import RateLimiter, RedisRateLimiter
    
    # 单机限流
    limiter = RateLimiter(rate=100, burst=200)
    if await limiter.acquire():
        # 处理请求
    else:
        # 拒绝请求（429 Too Many Requests）
    
    # Redis 分布式限流
    limiter = RedisRateLimiter(redis_client, key="api:user:123", rate=100, burst=200)
    if await limiter.acquire():
        ...
    
    # FastAPI 依赖注入
    @app.get("/api/chat")
    async def chat(limiter: RateLimiter = Depends(get_rate_limiter)):
        if not await limiter.acquire():
            raise HTTPException(status_code=429, detail="Too Many Requests")
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    """限流异常"""
    def __init__(self, retry_after: float = 0):
        self.retry_after = retry_after
        super().__init__(f"Rate limit exceeded, retry after {retry_after:.1f}s")


class RateLimiter:
    """令牌桶限流器（单机内存版）
    
    原理：
    - 桶容量 = burst（突发容量）
    - 每秒补充 rate 个令牌
    - 每次请求消耗 1 个令牌
    - 桶满时多余令牌丢弃
    
    Args:
        rate: 每秒补充的令牌数（稳态速率）
        burst: 桶容量（突发容量）
    """
    
    def __init__(self, rate: float, burst: int):
        self.rate = rate
        self.burst = burst
        self.tokens = float(burst)
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()
    
    async def acquire(self, tokens: int = 1) -> bool:
        """尝试获取令牌
        
        Args:
            tokens: 需要的令牌数
            
        Returns:
            True 表示获取成功，False 表示被限流
        """
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            
            # 补充令牌
            self.tokens = min(
                self.burst,
                self.tokens + elapsed * self.rate,
            )
            self.last_refill = now
            
            # 检查是否有足够令牌
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            
            return False
    
    async def wait_and_acquire(self, tokens: int = 1, timeout: float = 30.0) -> bool:
        """等待并获取令牌（阻塞直到获取成功或超时）
        
        Args:
            tokens: 需要的令牌数
            timeout: 最大等待时间（秒）
            
        Returns:
            True 表示获取成功，False 表示超时
        """
        deadline = time.monotonic() + timeout
        
        while time.monotonic() < deadline:
            if await self.acquire(tokens):
                return True
            
            # 计算需要等待的时间
            needed = tokens - self.tokens
            # rate<=0 表示令牌不再补充：原先 needed/self.rate 会 ZeroDivisionError
            if self.rate <= 0:
                wait_time = 0.1
            else:
                wait_time = needed / self.rate
            await asyncio.sleep(max(min(wait_time, 0.1), 0.001))
        
        return False
    
    def retry_after(self) -> float:
        """计算需要等待的时间（秒）"""
        if self.tokens >= 1:
            return 0.0
        needed = 1 - self.tokens
        # rate<=0：令牌永不补充，原先 needed/self.rate 会 ZeroDivisionError
        if self.rate <= 0:
            return float("inf")
        return needed / self.rate
    
    @property
    def available_tokens(self) -> float:
        """当前可用令牌数"""
        now = time.monotonic()
        elapsed = now - self.last_refill
        return min(self.burst, self.tokens + elapsed * self.rate)


class RedisRateLimiter:
    """Redis 分布式令牌桶限流器
    
    使用 Redis Lua 脚本实现原子操作，支持多实例部署。
    
    Args:
        redis_client: Redis 客户端实例
        key: 限流键名（如 "ratelimit:api:user:123"）
        rate: 每秒补充的令牌数
        burst: 桶容量
    """
    
    # Lua 脚本：原子化令牌桶操作
    LUA_SCRIPT = """
    local key = KEYS[1]
    local rate = tonumber(ARGV[1])
    local burst = tonumber(ARGV[2])
    local now = tonumber(ARGV[3])
    local requested = tonumber(ARGV[4])
    
    local last_refill = tonumber(redis.call('hget', key, 'last_refill') or now)
    local tokens = tonumber(redis.call('hget', key, 'tokens') or burst)
    
    -- 补充令牌
    local elapsed = math.max(0, now - last_refill)
    tokens = math.min(burst, tokens + elapsed * rate)
    
    -- 检查并消耗
    local allowed = 0
    if tokens >= requested then
        tokens = tokens - requested
        allowed = 1
    end
    
    -- 更新状态
    redis.call('hset', key, 'tokens', tokens)
    redis.call('hset', key, 'last_refill', now)
    redis.call('expire', key, math.ceil(burst / rate) + 10)
    
    return {allowed, tokens}
    """
    
    def __init__(self, redis_client, key: str, rate: float, burst: int):
        self.redis = redis_client
        self.key = f"ratelimit:{key}"
        self.rate = rate
        self.burst = burst
        self._script = None
    
    async def _get_script(self):
        """获取或注册 Lua 脚本"""
        if self._script is None:
            self._script = self.redis.register_script(self.LUA_SCRIPT)
        return self._script
    
    async def acquire(self, tokens: int = 1) -> bool:
        """尝试获取令牌"""
        try:
            script = await self._get_script()
            now = time.time()
            result = await script(
                keys=[self.key],
                args=[self.rate, self.burst, now, tokens],
            )
            allowed, remaining = result
            return bool(allowed)
        except Exception as e:
            logger.warning("Redis rate limiter error: %s, allowing request", e)
            return True  # Redis 故障时放行请求
    
    async def get_info(self) -> dict:
        """获取限流器状态"""
        try:
            info = await self.redis.hgetall(self.key)
            return {
                "key": self.key,
                "tokens": float(info.get(b"tokens", self.burst)),
                "last_refill": float(info.get(b"last_refill", 0)),
                "rate": self.rate,
                "burst": self.burst,
            }
        except Exception:
            return {"key": self.key, "error": "failed to get info"}


class SlidingWindowRateLimiter:
    """滑动窗口限流器（Redis 版）
    
    比令牌桶更精确，适合需要严格控制时间窗口的场景。
    
    Args:
        redis_client: Redis 客户端实例
        key: 限流键名
        max_requests: 窗口内最大请求数
        window_seconds: 窗口大小（秒）
    """
    
    LUA_SCRIPT = """
    local key = KEYS[1]
    local max_requests = tonumber(ARGV[1])
    local window = tonumber(ARGV[2])
    local now = tonumber(ARGV[3])
    
    -- 移除窗口外的请求
    redis.call('zremrangebyscore', key, 0, now - window)
    
    -- 统计当前窗口内的请求数
    local count = redis.call('zcard', key)
    
    if count < max_requests then
        redis.call('zadd', key, now, now .. '-' .. math.random(1000000))
        redis.call('expire', key, math.ceil(window))
        return {1, max_requests - count - 1}
    end
    
    return {0, 0}
    """
    
    def __init__(self, redis_client, key: str, max_requests: int, window_seconds: int):
        self.redis = redis_client
        self.key = f"sliding:{key}"
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._script = None
    
    async def acquire(self) -> bool:
        """尝试获取请求许可"""
        try:
            if self._script is None:
                self._script = self.redis.register_script(self.LUA_SCRIPT)
            
            now = time.time()
            result = await self._script(
                keys=[self.key],
                args=[self.max_requests, self.window_seconds, now],
            )
            allowed, remaining = result
            return bool(allowed)
        except Exception as e:
            logger.warning("Sliding window rate limiter error: %s", e)
            return True


# ============================================================
# FastAPI 集成
# ============================================================

def create_rate_limit_dependency(
    rate: float = 100,
    burst: int = 200,
    key_func=None,
):
    """创建 FastAPI 限流依赖
    
    用法：
        limiter_dep = create_rate_limit_dependency(rate=100, burst=200)
        
        @app.post("/chat")
        async def chat(limiter=Depends(limiter_dep)):
            ...
    """
    # 单机限流器（每个进程独立）
    _limiter = RateLimiter(rate=rate, burst=burst)
    
    async def dependency():
        if not await _limiter.acquire():
            retry = _limiter.retry_after()
            raise RateLimitExceeded(retry_after=retry)
        return _limiter
    
    return dependency
