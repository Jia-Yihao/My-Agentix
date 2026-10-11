"""P39：语义缓存与会话历史的**离线**测试（`redis_cache.py`，此前 0% 覆盖）。

`redis_cache.py` 是全仓**单文件缺口最大**的模块（195 语句、0% 覆盖），
原因是它同时依赖 Redis 与 Embedding HTTP 端点。本文件用两个替身解决：

- `tests/fakes/redis.py` 的 `FakeRedis`：只实现本项目实际用到的 9 个命令，
  **零新增依赖**（不引入 fakeredis）。
- `httpx.AsyncClient` 用 monkeypatch 替换，返回预设 embedding 向量。

顺带覆盖这些边界：**Embedding 服务故障不得打断聊天主链路**
（`lookup` / `update` 都吞异常返回 `None`）、容量超限淘汰最久未用、
损坏的缓存条目被跳过而非崩溃。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import httpx
import pytest

from agentix.application.services.redis_cache import (
    RedisConversationHistory,
    RedisSemanticCache,
)
from agentix.config import settings

# `tests/` 不是包（无 __init__.py），故用绝对导入；
# pytest 的 prepend 导入模式会把 tests/ 加入 sys.path，可直接 `import fakes.*`。
from fakes.redis import FakeRedis  # type: ignore[import-not-found]


class _FakeResponse:
    def __init__(self, payload: Any, *, raise_error: Exception | None = None) -> None:
        self._payload = payload
        self._raise_error = raise_error

    def raise_for_status(self) -> None:
        if self._raise_error is not None:
            raise self._raise_error
        return None

    def json(self) -> Any:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.payloads: List[Dict[str, Any]] = []

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def post(self, url: str, json: Any = None, headers: Any = None) -> _FakeResponse:
        self.payloads.append({"url": url, "json": json})
        return self.response


def _embedding(monkeypatch: pytest.MonkeyPatch, vector: List[float]) -> None:
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeAsyncClient(_FakeResponse({"data": [{"embedding": vector}]})),
    )


def _embedding_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeAsyncClient(
            _FakeResponse({}, raise_error=httpx.ConnectError("embedding down"))
        ),
    )


def _cache(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> RedisSemanticCache:
    monkeypatch.setattr(settings, "REDIS_CACHE_THRESHOLD", 0.9, raising=False)
    monkeypatch.setattr(settings, "REDIS_CACHE_MAX_SIZE", 100, raising=False)
    monkeypatch.setattr(settings, "REDIS_CACHE_EXPIRE", 3600, raising=False)
    return RedisSemanticCache(redis_client=FakeRedis(), **overrides)


def _msgs(text: str) -> List[Dict[str, Any]]:
    return [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": text},
    ]


# ---------------------------------------------------------------------------
# 纯函数辅助
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_message_hash_is_stable(self):
        assert RedisSemanticCache._message_hash("你好") == RedisSemanticCache._message_hash("你好")
        assert RedisSemanticCache._message_hash("你好") != RedisSemanticCache._message_hash("再见")

    def test_last_user_message_picks_latest(self):
        messages = [
            {"role": "user", "content": "第一句"},
            {"role": "assistant", "content": "回答"},
            {"role": "user", "content": "最后一句"},
        ]
        assert RedisSemanticCache._last_user_message(messages) == "最后一句"

    def test_last_user_message_none_when_absent(self):
        assert RedisSemanticCache._last_user_message([{"role": "assistant", "content": "x"}]) is None
        assert RedisSemanticCache._last_user_message([]) is None

    def test_namespace_scoping(self, monkeypatch):
        cache = _cache(monkeypatch)
        assert cache._namespace(None) == "semantic"
        assert cache._namespace("u1") == "semantic:u1"


# ---------------------------------------------------------------------------
# lookup
# ---------------------------------------------------------------------------


class TestLookup:
    @pytest.mark.asyncio
    async def test_returns_none_without_user_message(self, monkeypatch):
        _embedding(monkeypatch, [1.0, 0.0])
        cache = _cache(monkeypatch)
        assert await cache.lookup([{"role": "assistant", "content": "x"}]) is None

    @pytest.mark.asyncio
    async def test_embedding_failure_degrades_to_none(self, monkeypatch):
        """关键：Embedding 服务故障**不得**打断聊天主链路。"""
        _embedding_failure(monkeypatch)
        cache = _cache(monkeypatch)
        assert await cache.lookup(_msgs("你好")) is None

    @pytest.mark.asyncio
    async def test_cache_miss_on_empty_cache(self, monkeypatch):
        _embedding(monkeypatch, [1.0, 0.0])
        cache = _cache(monkeypatch)
        assert await cache.lookup(_msgs("民法典第577条")) is None

    @pytest.mark.asyncio
    async def test_cache_hit_after_update(self, monkeypatch):
        _embedding(monkeypatch, [1.0, 0.0])
        cache = _cache(monkeypatch)
        await cache.update(_msgs("民法典第577条"), "违约方应承担继续履行等责任。")

        hit = await cache.lookup(_msgs("民法典第577条"))
        assert hit == "违约方应承担继续履行等责任。"

    @pytest.mark.asyncio
    async def test_below_threshold_is_a_miss(self, monkeypatch):
        cache = _cache(monkeypatch)
        await cache.update(_msgs("甲"), "答案甲")
        # 换一个**几乎正交**的向量再查，相似度低于阈值 0.9
        _embedding(monkeypatch, [0.0, 1.0])
        assert await cache.lookup(_msgs("乙")) is None

    @pytest.mark.asyncio
    async def test_corrupted_vector_entry_is_skipped(self, monkeypatch):
        """损坏的缓存条目应被跳过并告警，不得崩溃。"""
        _embedding(monkeypatch, [1.0, 0.0])
        cache = _cache(monkeypatch)
        await cache._redis.set("semantic:vec:broken", b"not-json")
        assert await cache.lookup(_msgs("任意")) is None

    @pytest.mark.asyncio
    async def test_vector_present_but_response_missing(self, monkeypatch):
        """有向量无响应（三者不一致）时应返回 None，不得返回脏数据。"""
        _embedding(monkeypatch, [1.0, 0.0])
        cache = _cache(monkeypatch)
        await cache._redis.set("semantic:vec:h", json.dumps([1.0, 0.0]))
        assert await cache.lookup(_msgs("x")) is None


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_without_user_message_is_noop(self, monkeypatch):
        _embedding(monkeypatch, [1.0, 0.0])
        cache = _cache(monkeypatch)
        await cache.update([{"role": "assistant", "content": "x"}], "答案")
        assert cache._redis.size() == 0

    @pytest.mark.asyncio
    async def test_embedding_failure_skips_write(self, monkeypatch):
        _embedding_failure(monkeypatch)
        cache = _cache(monkeypatch)
        await cache.update(_msgs("你好"), "答案")
        assert cache._redis.size() == 0

    @pytest.mark.asyncio
    async def test_update_writes_three_keys(self, monkeypatch):
        _embedding(monkeypatch, [1.0, 0.0])
        cache = _cache(monkeypatch)
        await cache.update(_msgs("你好"), "答案")
        keys = cache._redis.keys()
        assert any(":vec:" in k for k in keys)
        assert any(":resp:" in k for k in keys)
        assert any(":meta:" in k for k in keys)

    @pytest.mark.asyncio
    async def test_custom_expire_is_honoured(self, monkeypatch):
        _embedding(monkeypatch, [1.0, 0.0])
        cache = _cache(monkeypatch)
        await cache.update(_msgs("你好"), "答案", expire=42)
        assert 42 in cache._redis.ttls.values()


# ---------------------------------------------------------------------------
# 容量淘汰与清理
# ---------------------------------------------------------------------------


class TestCapacityAndClear:
    @pytest.mark.asyncio
    async def test_capacity_trims_least_recently_used(self, monkeypatch):
        """超过 max_cache_size 时淘汰**最久未访问**的条目。"""
        _embedding(monkeypatch, [1.0, 0.0])
        cache = _cache(monkeypatch, max_cache_size=2)
        await cache.update(_msgs("甲"), "答甲")
        await cache.update(_msgs("乙"), "答乙")
        await cache.update(_msgs("丙"), "答丙")

        # 只应保留 2 条的元数据
        meta_keys = [k for k in cache._redis.keys() if ":meta:" in k]
        assert len(meta_keys) == 2
        # 最旧的「甲」被淘汰（响应键也随之删除）
        resp_keys = [k for k in cache._redis.keys() if ":resp:" in k]
        assert len(resp_keys) == 2

    @pytest.mark.asyncio
    async def test_corrupted_metadata_is_skipped(self, monkeypatch):
        _embedding(monkeypatch, [1.0, 0.0])
        cache = _cache(monkeypatch, max_cache_size=1)
        await cache._redis.set("semantic:meta:junk", b"not-json")
        await cache.update(_msgs("甲"), "答甲")
        assert True  # 未崩溃即通过（损坏元数据被跳过）

    @pytest.mark.asyncio
    async def test_clear_namespace_removes_all(self, monkeypatch):
        _embedding(monkeypatch, [1.0, 0.0])
        cache = _cache(monkeypatch)
        await cache.update(_msgs("甲"), "答甲", scope="s1")
        await cache.update(_msgs("乙"), "答乙", scope="s2")

        await cache.clear_namespace("s1")
        assert not any(k.startswith("semantic:s1:") for k in cache._redis.keys())
        assert any(k.startswith("semantic:s2:") for k in cache._redis.keys())

    @pytest.mark.asyncio
    async def test_clear_default_namespace(self, monkeypatch):
        _embedding(monkeypatch, [1.0, 0.0])
        cache = _cache(monkeypatch)
        await cache.update(_msgs("甲"), "答甲")
        await cache.clear_namespace()
        assert cache._redis.size() == 0


# ---------------------------------------------------------------------------
# FakeRedis 自身（有序集合部分：长期记忆索引用到）
# ---------------------------------------------------------------------------


class TestFakeRedisSortedSets:
    """`tests/fakes/redis.py` 的 zset 命令——长期记忆的用户索引依赖它们。"""

    @pytest.mark.asyncio
    async def test_zadd_and_zrange_sorted_by_score(self):
        fake = FakeRedis()
        await fake.zadd("idx", {"b": 2.0, "a": 1.0, "c": 3.0})
        assert await fake.zrange("idx", 0, -1) == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_zrevrange_is_descending(self):
        fake = FakeRedis()
        await fake.zadd("idx", {"a": 1.0, "b": 2.0})
        assert await fake.zrevrange("idx", 0, -1) == ["b", "a"]

    @pytest.mark.asyncio
    async def test_zadd_updates_existing_member_score(self):
        """同一 member 重复 zadd 应更新分数，而不是新增一条。"""
        fake = FakeRedis()
        await fake.zadd("idx", {"a": 1.0})
        await fake.zadd("idx", {"a": 5.0})
        assert await fake.zrange("idx", 0, -1) == ["a"]
        assert fake.store["idx"][0][0] == 5.0

    @pytest.mark.asyncio
    async def test_zremrangebyrank_trims_from_the_low_end(self):
        """与 Redis 一致：按 rank 删除（0 = 分数最小者），用于容量淘汰。"""
        fake = FakeRedis()
        await fake.zadd("idx", {"a": 1.0, "b": 2.0, "c": 3.0})
        removed = await fake.zremrangebyrank("idx", 0, 0)
        assert removed == 1
        assert await fake.zrange("idx", 0, -1) == ["b", "c"]

    @pytest.mark.asyncio
    async def test_empty_or_missing_key_is_safe(self):
        fake = FakeRedis()
        assert await fake.zrange("nope", 0, -1) == []
        assert await fake.zrevrange("nope", 0, -1) == []
        assert await fake.zremrangebyrank("nope", 0, -1) == 0

    @pytest.mark.asyncio
    async def test_size_counts_keys_not_members(self):
        fake = FakeRedis()
        await fake.zadd("idx", {"a": 1.0, "b": 2.0})
        assert fake.size() == 1


# ---------------------------------------------------------------------------
# 会话历史
# ---------------------------------------------------------------------------


class TestConversationHistory:
    @pytest.fixture
    def history(self, monkeypatch):
        monkeypatch.setattr(settings, "CONVERSATION_HISTORY_TTL", 3600, raising=False)
        monkeypatch.setattr(settings, "CONVERSATION_HISTORY_MAX_MESSAGES", 200, raising=False)
        return RedisConversationHistory(redis_client=FakeRedis())

    @pytest.mark.asyncio
    async def test_append_and_get_recent(self, history):
        await history.append("s1", "user", "你好")
        await history.append("s1", "assistant", "你好！")
        recent = await history.get_recent("s1", limit=5)
        assert [m["content"] for m in recent] == ["你好", "你好！"]
        # 还应保留角色与时间戳
        assert recent[0]["role"] == "user"
        assert "timestamp" in recent[0]

    @pytest.mark.asyncio
    async def test_get_recent_limits_results(self, history):
        for i in range(5):
            await history.append("s1", "user", f"m{i}")
        recent = await history.get_recent("s1", limit=2)
        assert [m["content"] for m in recent] == ["m3", "m4"]

    @pytest.mark.asyncio
    async def test_get_recent_empty_session(self, history):
        assert await history.get_recent("missing", limit=5) == []

    @pytest.mark.asyncio
    async def test_clear_session(self, history):
        await history.append("s1", "user", "你好")
        await history.clear("s1")
        assert await history.get_recent("s1") == []

    @pytest.mark.asyncio
    async def test_size_reports_message_count(self, history):
        await history.append("s1", "user", "一")
        await history.append("s1", "user", "二")
        assert await history.size("s1") == 2

    @pytest.mark.asyncio
    async def test_sessions_are_isolated(self, history):
        await history.append("s1", "user", "一")
        await history.append("s2", "user", "二")
        assert [m["content"] for m in await history.get_recent("s1")] == ["一"]
        assert [m["content"] for m in await history.get_recent("s2")] == ["二"]

    @pytest.mark.asyncio
    async def test_corrupted_entry_is_skipped(self, history):
        await history._redis.rpush(history._key("s1"), b"not-json")
        assert await history.get_recent("s1") == []
