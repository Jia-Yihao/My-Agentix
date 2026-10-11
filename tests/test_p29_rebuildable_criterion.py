"""批次 10（§10.10.2）——可重建压缩判据（纯函数先行）守卫测试。

验证：
- ``is_rebuildable`` 的包含判定、空真防护（空集/无 id 一律 False）；
- ``MemorySummary.source_message_ids`` 溯源字段的序列化 roundtrip；
- ``store_summary`` 的溯源默认提取（末 20 条窗口内带 id 的消息）。

运行：./.venv/Scripts/python.exe -m pytest tests/test_p29_rebuildable_criterion.py -q
"""
import pytest

from agentix.application.services.long_term_memory import (
    LongTermMemory,
    MemorySummary,
    is_rebuildable,
)


def _summary_with_ids(ids):
    return MemorySummary(
        session_id="s1",
        user_id="u1",
        summary="摘要",
        source_message_ids=list(ids),
    )


class TestIsRebuildable:
    def test_fully_covered_items_are_rebuildable(self):
        items = [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]
        assert is_rebuildable(items, _summary_with_ids(["m1", "m2", "m3"])) is True

    def test_fully_covered_is_true_even_with_extra_source_ids(self):
        items = [{"id": "m1"}, {"id": "m2"}]
        assert is_rebuildable(items, _summary_with_ids(["m1", "m2", "m9"])) is True

    def test_uncovered_item_makes_it_not_rebuildable(self):
        items = [{"id": "m1"}, {"id": "mX"}]
        assert is_rebuildable(items, _summary_with_ids(["m1", "m2"])) is False

    def test_empty_items_is_false_vacuous_truth_guard(self):
        """空真防护：没有待判定的消息 ≠ 已覆盖，必须 False。"""
        assert is_rebuildable([], _summary_with_ids(["m1"])) is False

    def test_items_without_ids_is_false_conservative_default(self):
        """无 id 的消息无法溯源，永远不可判定为可重建（保守默认）。"""
        items = [{"role": "user", "content": "no id here"}]
        assert is_rebuildable(items, _summary_with_ids(["m1"])) is False

    def test_empty_summary_coverage_is_false(self):
        items = [{"id": "m1"}]
        assert is_rebuildable(items, _summary_with_ids([])) is False

    def test_int_ids_are_normalized_to_str(self):
        items = [{"id": 7}]
        assert is_rebuildable(items, _summary_with_ids(["7"])) is True


class TestMemorySummarySourceIds:
    def test_field_defaults_to_empty(self):
        mem = MemorySummary(session_id="s", user_id="u", summary="x")
        assert mem.source_message_ids == []

    def test_roundtrip_preserves_source_ids(self):
        mem = _summary_with_ids(["m1", "m2", "m3"])
        restored = MemorySummary.from_dict(mem.to_dict())
        assert restored.source_message_ids == ["m1", "m2", "m3"]

    def test_from_dict_without_field_is_backward_compatible(self):
        """旧格式（无 source_message_ids 键）反序列化必须兼容（默认空表）。"""
        mem = MemorySummary.from_dict(
            {"session_id": "s", "user_id": "u", "summary": "x"}
        )
        assert mem.source_message_ids == []


class _StoreFakeRedis:
    """只实现 store_summary 用到的四个命令的最小 Redis 假件。"""

    def __init__(self):
        self.store = {}
        self.expired = []

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.expired.append((key, ex))
        return True

    async def get(self, key):
        return self.store.get(key)

    async def zadd(self, key, mapping):
        self.store.setdefault(key, {}).update(mapping)
        return 1

    async def zremrangebyrank(self, key, start, stop):
        return 0

    async def expire(self, key, ttl):
        return True


class TestStoreSummarySourceExtraction:
    @pytest.mark.asyncio
    async def test_default_derivation_uses_last_20_window(self):
        redis = _StoreFakeRedis()
        ltm = LongTermMemory(redis_client=redis, summary_ttl=60)
        messages = [{"id": f"m{i}", "role": "user", "content": "c"} for i in range(25)]

        mem = await ltm.store_summary("sess1", "摘要", messages)

        # 窗口 = 末 20 条 → m5..m24（前 5 条不在摘要覆盖内）
        assert mem.source_message_ids == [f"m{i}" for i in range(5, 25)]

    @pytest.mark.asyncio
    async def test_messages_without_ids_yield_empty_trace(self):
        redis = _StoreFakeRedis()
        ltm = LongTermMemory(redis_client=redis, summary_ttl=60)

        mem = await ltm.store_summary(
            "sess1", "摘要", [{"role": "user", "content": "no id"}]
        )

        assert mem.source_message_ids == []
        assert is_rebuildable(
            [{"role": "user", "content": "no id"}], mem
        ) is False

    @pytest.mark.asyncio
    async def test_explicit_ids_win_over_derivation(self):
        redis = _StoreFakeRedis()
        ltm = LongTermMemory(redis_client=redis, summary_ttl=60)

        mem = await ltm.store_summary(
            "sess1",
            "摘要",
            [{"id": "m1"}, {"id": "m2"}],
            source_message_ids=["m1"],
        )

        assert mem.source_message_ids == ["m1"]

    @pytest.mark.asyncio
    async def test_persisted_json_contains_source_ids(self):
        redis = _StoreFakeRedis()
        ltm = LongTermMemory(redis_client=redis, summary_ttl=60)

        await ltm.store_summary(
            "sess1", "摘要", [{"id": "m1"}], source_message_ids=["m1"]
        )

        import json

        raw = json.loads(redis.store["ltm:summary:sess1"])
        assert raw["source_message_ids"] == ["m1"]
