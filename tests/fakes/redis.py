"""内存版 Redis 替身（仅实现本项目实际用到的命令子集）。

**为什么自己写而不用 fakeredis**：本仓库并未引入该依赖，而 `redis_cache.py`
只用到 9 个命令（`set` / `get` / `delete` / `scan_iter` / `rpush` / `ltrim` /
`lrange` / `llen` / `expire`），自写替身可**零新增依赖**且行为完全可控——
这对"离线可跑 + 确定性"比"命令覆盖全"更重要。

用法：直接把实例当 `redis.asyncio.Redis` 传入被测对象。
"""

from __future__ import annotations

import fnmatch
from typing import Any, Dict, List, Optional


class FakeRedis:
    """最小可用的异步 Redis 替身（键值 + 列表）。"""

    def __init__(self) -> None:
        self.store: Dict[str, Any] = {}
        self.hashes: Dict[str, Dict[Any, Any]] = {}
        self.ttls: Dict[str, int] = {}

    # -- 基本键值 -----------------------------------------------------------

    async def set(self, key: str, value: Any, ex: Optional[int] = None, nx: bool = False) -> bool:
        # NX 语义与真实 Redis 一致：键已存在则不写、返回 False（SET NX EX 原子声明）
        if nx and key in self.store:
            return False
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def get(self, key: str) -> Any:
        return self.store.get(key)

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                self.ttls.pop(key, None)
                removed += 1
            if key in self.hashes:
                del self.hashes[key]
                self.ttls.pop(key, None)
                removed += 1
        return removed

    async def expire(self, key: str, seconds: int) -> bool:
        if key not in self.store and key not in self.hashes:
            return False
        self.ttls[key] = seconds
        return True

    # -- 列表 ---------------------------------------------------------------

    async def rpush(self, key: str, *values: Any) -> int:
        bucket = self.store.setdefault(key, [])
        if not isinstance(bucket, list):
            bucket = []
            self.store[key] = bucket
        bucket.extend(values)
        return len(bucket)

    async def ltrim(self, key: str, start: int, stop: int) -> bool:
        bucket = self.store.get(key)
        if not isinstance(bucket, list):
            return True
        # Redis 语义：stop 为闭区间；-1 表示末尾
        end = len(bucket) if stop < 0 else stop + 1
        self.store[key] = bucket[start:end]
        return True

    async def lrange(self, key: str, start: int, stop: int) -> List[Any]:
        bucket = self.store.get(key)
        if not isinstance(bucket, list):
            return []
        end = len(bucket) if stop < 0 else stop + 1
        return bucket[start:end]

    async def llen(self, key: str) -> int:
        bucket = self.store.get(key)
        return len(bucket) if isinstance(bucket, list) else 0

    # -- 哈希（hash）---------------------------------------------------------
    # 与 decode_responses=True 的真实行为一致：按原样存取（调用方均存字符串）。

    async def hset(self, key: str, field: str = None, value: Any = None, mapping: Optional[Dict[Any, Any]] = None) -> int:
        h = self.hashes.setdefault(key, {})
        added = 0
        if mapping:
            for f, v in mapping.items():
                if f not in h:
                    added += 1
                h[f] = v
        if field is not None:
            if field not in h:
                added += 1
            h[field] = value
        return added

    async def hget(self, key: str, field: Any) -> Optional[Any]:
        return self.hashes.get(key, {}).get(field)

    async def hgetall(self, key: str) -> Dict[Any, Any]:
        return dict(self.hashes.get(key, {}))

    # -- pipeline ------------------------------------------------------------
    # 顺序执行、收集返回值（不做原子性保证——单测语义足够，且可确定性回放）。

    def pipeline(self) -> "_Pipeline":
        return _Pipeline(self)

    # -- 有序集合（sorted set）----------------------------------------------
    # 底层用「按分数升序的 [(score, member)] 列表」表示，够用且行为可预期。
    # 注意：同一 member 重复 zadd 视为**更新分数**（与 Redis 一致）。

    async def zadd(self, key: str, mapping: Dict[Any, float]) -> int:
        bucket = self.store.get(key)
        if not isinstance(bucket, list):
            bucket = []
            self.store[key] = bucket
        added = 0
        for member, score in mapping.items():
            existing = next((i for i, (_, m) in enumerate(bucket) if m == member), None)
            if existing is None:
                bucket.append((float(score), member))
                added += 1
            else:
                bucket[existing] = (float(score), member)
        self._sort_zset(key)
        return added

    def _sort_zset(self, key: str) -> None:
        bucket = self.store.get(key)
        if isinstance(bucket, list) and bucket and isinstance(bucket[0], tuple):
            bucket.sort(key=lambda item: item[0])
            self.store[key] = bucket

    async def zrange(self, key: str, start: int, stop: int) -> List[Any]:
        bucket = self.store.get(key)
        if not isinstance(bucket, list) or not bucket:
            return []
        n = len(bucket)
        start_i = start if start >= 0 else max(0, n + start)
        # stop 为负 = 从末尾数起（-1 = 最后一个），与真实 Redis 一致
        end = n if stop < 0 else min(stop + 1, n)
        if stop < 0:
            end = max(0, n + stop + 1)
        if start_i >= end:
            return []
        return [m for _, m in bucket[start_i:end]]

    async def zrevrange(self, key: str, start: int, stop: int) -> List[Any]:
        forward = await self.zrange(key, start, stop)
        return list(reversed(forward))

    async def zremrangebyrank(self, key: str, start: int, stop: int) -> int:
        bucket = self.store.get(key)
        if not isinstance(bucket, list) or not bucket:
            return 0
        n = len(bucket)
        start_i = start if start >= 0 else max(0, n + start)
        end = n if stop < 0 else min(stop + 1, n)
        if stop < 0:
            end = max(0, n + stop + 1)
        if start_i >= end:
            # 区间空（如 stop=-201 在小集合上越界）⇒ 不删任何元素
            return 0
        removed = bucket[start_i:end]
        del bucket[start_i:end]
        return len(removed)

    # -- 扫描 ---------------------------------------------------------------

    async def scan_iter(self, match: Optional[str] = None, count: int = 100):
        """同步生成器（被测代码用 `async for`，两者皆可迭代）。"""
        for key in list(self.store):
            if match is None or fnmatch.fnmatch(key, match):
                yield key

    # -- 测试辅助 -----------------------------------------------------------

    # ⚠️ **不要**定义 `__len__`：被测代码普遍写 `redis_client or Redis.from_url(...)`，
    # 而空实例若有 `__len__` 返回 0 就会被判为 falsy，从而**静默回退到真实 Redis**
    # （实测表现为 `ConnectionError: redis:6379`）。这个坑实际踩过一次，此处钉死说明。

    def size(self) -> int:
        """条目数（用方法而非 `__len__`，避免上面的 falsy 陷阱）。"""
        return len(self.store)

    def keys(self) -> List[str]:
        return list(self.store)

    async def close(self) -> None:
        return None


class _Pipeline:
    """顺序收集命令、execute() 时依次执行并返回结果列表。"""

    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._calls = []

    def hgetall(self, key: str) -> "_Pipeline":
        self._calls.append(lambda: self._redis.hgetall(key))
        return self

    def hset(self, key: str, field: str = None, value: Any = None, mapping: Optional[Dict[Any, Any]] = None) -> "_Pipeline":
        self._calls.append(lambda: self._redis.hset(key, field=field, value=value, mapping=mapping))
        return self

    def hget(self, key: str, field: Any) -> "_Pipeline":
        self._calls.append(lambda: self._redis.hget(key, field))
        return self

    def expire(self, key: str, seconds: int) -> "_Pipeline":
        self._calls.append(lambda: self._redis.expire(key, seconds))
        return self

    async def execute(self) -> List[Any]:
        return [await call() for call in self._calls]
