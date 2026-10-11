"""P30：上下文隔离通道（artifact store）+ 记忆整理与可重建性淘汰。

覆盖《My Agentix_架构优化方案》§9.1 C 组缺口：
- 「存在上下文隔离通道承载大中间产物」
- 「有遗忘/整理机制」
- 「上下文压缩有『可重建性』判据」（本次接进淘汰路径）

全部离线，零外部依赖。
"""

from __future__ import annotations

import time

import pytest

from agentix.application.services.artifact_store import (
    ArtifactRef,
    InMemoryArtifactStore,
    offload_content,
)
from agentix.application.services.long_term_memory import MemorySummary, is_rebuildable
from agentix.application.services.memory_consolidation import (
    EvictionPlan,
    merge_summaries,
    plan_message_eviction,
    plan_summary_eviction,
    select_consolidation_groups,
    summarize_coverage,
)

DAY = 86400.0


# ---------------------------------------------------------------------------
# 上下文隔离通道
# ---------------------------------------------------------------------------


class TestArtifactStore:
    def setup_method(self):
        self.store = InMemoryArtifactStore(max_items=10, max_bytes=10_000)

    def test_put_get_roundtrip_is_lossless(self):
        payload = "x" * 5000
        ref = self.store.put(payload, kind="cypher_rows", summary="命中 137 条")
        assert isinstance(ref, ArtifactRef)
        assert ref.size_bytes == 5000
        assert ref.kind == "cypher_rows"
        assert ref.summary == "命中 137 条"
        # 关键：取回的是**原文**（隔离无损，区别于压缩）
        assert self.store.get(ref.artifact_id) == payload

    def test_preview_is_bounded(self):
        ref = self.store.put("y" * 5000, preview_chars=50)
        assert len(ref.preview) == 51  # 50 + 省略号
        assert ref.preview.endswith("…")

    def test_short_content_preview_is_full(self):
        ref = self.store.put("abc", preview_chars=50)
        assert ref.preview == "abc"

    def test_delete_and_missing(self):
        ref = self.store.put("data")
        assert self.store.delete(ref.artifact_id) is True
        assert self.store.get(ref.artifact_id) is None
        assert self.store.delete(ref.artifact_id) is False

    def test_evicts_by_item_count(self):
        for i in range(15):
            self.store.put(f"item-{i}")
        assert len(self.store) == 10
        stats = self.store.stats()
        assert stats["evicted"] == 5
        assert stats["items"] == 10

    def test_evicts_by_bytes(self):
        store = InMemoryArtifactStore(max_items=100, max_bytes=1000)
        for _ in range(5):
            store.put("z" * 400)
        assert store.stats()["bytes"] <= 1000
        assert store.stats()["evicted"] >= 1

    def test_lru_touch_protects_recently_read(self):
        store = InMemoryArtifactStore(max_items=2, max_bytes=10**9)
        first = store.put("first")
        second = store.put("second")
        store.get(first.artifact_id)  # 触碰 → 变成最近使用
        third = store.put("third")    # 应淘汰 second 而非 first
        assert store.get(first.artifact_id) == "first"
        assert store.get(third.artifact_id) == "third"
        assert store.get(second.artifact_id) is None

    def test_unrebuildable_eviction_is_counted_separately(self):
        store = InMemoryArtifactStore(max_items=1, max_bytes=10**9)
        store.put("volatile", rebuildable=False)
        store.put("next")
        stats = store.stats()
        assert stats["evicted"] == 1
        assert stats["evicted_unrebuildable"] == 1

    def test_ref_roundtrip_dict(self):
        ref = self.store.put("abc", kind="k", meta={"rows": 3})
        restored = ArtifactRef.from_dict(ref.to_dict())
        assert restored == ref


class TestOffloadContent:
    def test_below_threshold_stays_inline(self):
        result = offload_content("short", inline_threshold=100)
        assert result.is_offloaded is False
        assert result.inline == "short"
        assert result.ref is None

    def test_above_threshold_is_offloaded(self):
        store = InMemoryArtifactStore()
        result = offload_content("a" * 500, store=store, inline_threshold=100)
        assert result.is_offloaded is True
        assert result.ref is not None
        assert store.get(result.ref.artifact_id) == "a" * 500

    def test_value_returns_preview_when_offloaded(self):
        store = InMemoryArtifactStore()
        result = offload_content("a" * 500, store=store, inline_threshold=100, preview_chars=20)
        assert result.value() == result.ref.preview

    def test_store_failure_degrades_to_inline(self):
        """可选增强的降级语义：外置失败不得阻断主链路。"""

        class Boom:
            def put(self, *a, **kw):
                raise RuntimeError("redis down")

        result = offload_content("b" * 500, store=Boom(), inline_threshold=100)
        assert result.is_offloaded is False
        assert result.inline == "b" * 500
        assert "redis down" in (result.degraded_reason or "")

    def test_store_returning_no_handle_degrades_to_inline(self):
        """边界：store 返回空句柄 ⇒ 必须降级内联，绝不静默丢内容。

        若此时仍返回 `is_offloaded=True`，内容会既不在 state、也取不回来。
        """
        class Broken:
            def put(self, *a, **k):
                return None

        result = offload_content("b" * 500, store=Broken(), inline_threshold=100)
        assert result.is_offloaded is False
        assert result.inline == "b" * 500
        assert "no handle" in (result.degraded_reason or "")

    def test_value_never_raises_on_inconsistent_state(self):
        """即使拿到不一致的结果（外置标志为真但无句柄），`value()` 也不抛异常。"""
        from agentix.application.services.artifact_store import OffloadResult

        broken = OffloadResult(is_offloaded=True, ref=None)
        assert broken.value() == ""

    def test_threshold_zero_disables_offload(self):
        result = offload_content("c" * 10_000, inline_threshold=0)
        assert result.is_offloaded is False


# ---------------------------------------------------------------------------
# 记忆整理（consolidation）
# ---------------------------------------------------------------------------


def _summary(sid: str, *, topics, created_at, msg_ids=(), access=0, text="s"):
    return MemorySummary(
        session_id=sid,
        user_id="u1",
        summary=text,
        topics=list(topics),
        key_facts=[],
        message_count=1,
        created_at=created_at,
        access_count=access,
        last_accessed=created_at,
        source_message_ids=list(msg_ids),
    )


class TestConsolidationGroups:
    def test_groups_same_topic_within_gap(self):
        now = time.time()
        summaries = [
            _summary("a", topics=["租赁"], created_at=now - 3 * DAY),
            _summary("b", topics=["租赁"], created_at=now - 2 * DAY),
            _summary("c", topics=["租赁"], created_at=now - 1 * DAY),
        ]
        plan = select_consolidation_groups(summaries, now=now, max_gap_days=7)
        assert plan.group_count == 1
        assert [s.session_id for s in plan.groups[0]] == ["a", "b", "c"]
        assert plan.merged_summary_count == 2

    def test_gap_breaks_group(self):
        now = time.time()
        summaries = [
            _summary("a", topics=["租赁"], created_at=now - 30 * DAY),
            _summary("b", topics=["租赁"], created_at=now - 1 * DAY),
        ]
        plan = select_consolidation_groups(summaries, now=now, max_gap_days=7)
        assert plan.group_count == 0
        assert len(plan.untouched) == 2

    def test_gap_zero_ignores_time(self):
        now = time.time()
        summaries = [
            _summary("a", topics=["租赁"], created_at=now - 300 * DAY),
            _summary("b", topics=["租赁"], created_at=now - 1 * DAY),
        ]
        plan = select_consolidation_groups(summaries, now=now, max_gap_days=0)
        assert plan.group_count == 1

    def test_different_topics_not_grouped(self):
        now = time.time()
        summaries = [
            _summary("a", topics=["租赁"], created_at=now - DAY),
            _summary("b", topics=["劳动"], created_at=now - DAY / 2),
        ]
        assert select_consolidation_groups(summaries, now=now).group_count == 0

    def test_no_topics_never_grouped(self):
        now = time.time()
        summaries = [
            _summary("a", topics=[], created_at=now - DAY),
            _summary("b", topics=[], created_at=now),
        ]
        assert select_consolidation_groups(summaries, now=now).group_count == 0

    def test_max_groups_caps_output(self):
        now = time.time()
        summaries = []
        for i in range(9):
            summaries.append(_summary(f"t{i}a", topics=[f"topic{i}"], created_at=now - DAY))
            summaries.append(_summary(f"t{i}b", topics=[f"topic{i}"], created_at=now))
        plan = select_consolidation_groups(summaries, now=now, max_groups=2)
        assert plan.group_count == 2


class TestMergeSummaries:
    def test_merge_unions_topics_and_facts(self):
        a = _summary("a", topics=["租赁", "押金"], created_at=100.0, msg_ids=["m1"], text="A")
        a.key_facts = ["押金可退"]
        b = _summary("b", topics=["租赁", "违约金"], created_at=200.0, msg_ids=["m2"], text="B")
        b.key_facts = ["押金可退", "违约金上限"]

        merged = merge_summaries([a, b])
        assert merged.topics == ["租赁", "押金", "违约金"]
        assert merged.key_facts == ["押金可退", "违约金上限"]
        # 溯源并集：合并后仍能证明覆盖了全部原始消息（可重建判据继续成立的前提）
        assert merged.source_message_ids == ["m1", "m2"]

    def test_merge_keeps_earliest_created_at(self):
        a = _summary("a", topics=["x"], created_at=100.0)
        b = _summary("b", topics=["x"], created_at=500.0)
        assert merge_summaries([a, b]).created_at == 100.0

    def test_merge_sums_counts(self):
        a = _summary("a", topics=["x"], created_at=1.0, access=2)
        a.message_count = 3
        b = _summary("b", topics=["x"], created_at=2.0, access=5)
        b.message_count = 4
        merged = merge_summaries([a, b])
        assert merged.message_count == 7
        assert merged.access_count == 7

    def test_merge_does_not_llm_summarize(self):
        """整理本身不做 LLM 再摘要（保持可审计，不引入新的不可逆变换）。"""
        a = _summary("a", topics=["x"], created_at=1.0, text="第一段")
        b = _summary("b", topics=["x"], created_at=2.0, text="第二段")
        merged = merge_summaries([a, b])
        assert "第一段" in merged.summary and "第二段" in merged.summary

    def test_merge_requires_input(self):
        with pytest.raises(ValueError):
            merge_summaries([])

    def test_merged_sources_still_satisfy_rebuildability(self):
        """整理后，被覆盖的原始消息**依然**可判定为可重建（不因合并而丢失证据）。"""
        msgs = [{"id": "m1"}, {"id": "m2"}]
        a = _summary("a", topics=["x"], created_at=1.0, msg_ids=["m1"])
        b = _summary("b", topics=["x"], created_at=2.0, msg_ids=["m2"])
        merged = merge_summaries([a, b])
        assert is_rebuildable(msgs, merged) is True


# ---------------------------------------------------------------------------
# 可重建性接进淘汰路径
# ---------------------------------------------------------------------------


class TestMessageEviction:
    def test_drops_only_covered_messages(self):
        messages = [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}, {"id": "m4"}]
        summary = _summary("s", topics=["x"], created_at=1.0, msg_ids=["m1", "m2"])
        plan = plan_message_eviction(messages, [summary], keep_last_n=0)
        assert [m["id"] for m in plan.drop] == ["m1", "m2"]
        assert [m["id"] for m in plan.retain] == ["m3", "m4"]
        assert plan.blocked == 2
        assert plan.covered_by == {"m1": "s", "m2": "s"}

    def test_keep_last_n_never_dropped(self):
        messages = [{"id": "m1"}, {"id": "m2"}]
        summary = _summary("s", topics=["x"], created_at=1.0, msg_ids=["m1", "m2"])
        plan = plan_message_eviction(messages, [summary], keep_last_n=1)
        assert [m["id"] for m in plan.drop] == ["m1"]

    def test_no_summaries_blocks_everything(self):
        """无覆盖证据 ⇒ 一律保留（保守默认，不是「可以删」）。"""
        messages = [{"id": "m1"}, {"id": "m2"}]
        plan = plan_message_eviction(messages, [], keep_last_n=0)
        assert plan.drop == []
        assert plan.blocked == 2

    def test_messages_without_id_are_never_dropped(self):
        """空真防护：缺 id 的消息永远不可判定为可重建。"""
        messages = [{"content": "no id"}, {"id": "m1"}]
        summary = _summary("s", topics=["x"], created_at=1.0, msg_ids=["m1"])
        plan = plan_message_eviction(messages, [summary], keep_last_n=0)
        assert [m.get("id") for m in plan.drop] == ["m1"]
        assert plan.blocked == 1

    def test_is_fully_covered_flag(self):
        messages = [{"id": "m1"}]
        summary = _summary("s", topics=["x"], created_at=1.0, msg_ids=["m1"])
        plan = plan_message_eviction(messages, [summary], keep_last_n=0)
        assert plan.is_fully_covered is True
        assert plan.dropped_count == 1

    def test_coverage_summary(self):
        messages = [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}, {"id": "m4"}]
        summary = _summary("s", topics=["x"], created_at=1.0, msg_ids=["m1"])
        plan = plan_message_eviction(messages, [summary], keep_last_n=0)
        cov = summarize_coverage(messages, plan)
        assert cov == {
            "total_candidates": 4,
            "covered": 1,
            "blocked": 3,
            "coverage_ratio": 0.25,
        }


class TestSummaryEviction:
    def test_expired_low_access_is_evicted(self):
        now = time.time()
        old = _summary("old", topics=["x"], created_at=now - 200 * DAY, access=0)
        fresh = _summary("fresh", topics=["x"], created_at=now - DAY)
        keep, evict = plan_summary_eviction([old, fresh], now=now, ttl_days=90)
        assert [s.session_id for s in evict] == ["old"]
        assert [s.session_id for s in keep] == ["fresh"]

    def test_accessed_memory_survives_ttl(self):
        now = time.time()
        old_but_used = _summary("used", topics=["x"], created_at=now - 200 * DAY, access=3)
        keep, evict = plan_summary_eviction([old_but_used], now=now, ttl_days=90)
        assert keep and not evict

    def test_capacity_evicts_least_used_first(self):
        now = time.time()
        summaries = [
            _summary("a", topics=["x"], created_at=now, access=0),
            _summary("b", topics=["x"], created_at=now, access=5),
            _summary("c", topics=["x"], created_at=now, access=1),
        ]
        keep, evict = plan_summary_eviction(summaries, now=now, ttl_days=0, max_items=2)
        assert [s.session_id for s in evict] == ["a"]
        assert {s.session_id for s in keep} == {"b", "c"}

    def test_ttl_zero_disables_expiry(self):
        now = time.time()
        old = _summary("old", topics=["x"], created_at=now - 9999 * DAY)
        keep, evict = plan_summary_eviction([old], now=now, ttl_days=0, max_items=0)
        assert keep and not evict

    def test_empty_input(self):
        keep, evict = plan_summary_eviction([])
        assert keep == [] and evict == []


class TestEvictionPlanDefaults:
    def test_empty_plan(self):
        plan = EvictionPlan()
        assert plan.dropped_count == 0
        assert plan.is_fully_covered is True


# ---------------------------------------------------------------------------
# 整理执行侧（LongTermMemory.consolidate）：把纯函数接进真实存储
# ---------------------------------------------------------------------------


class TestConsolidationDriver:
    """`memory_consolidation` 的纯函数此前**没有任何调用方**；
    `LongTermMemory.consolidate()` 是它的执行侧。

    用 `tests/fakes/redis.py` 的内存替身，全程离线、零外部依赖。
    """

    @staticmethod
    def _seed(mem, session_id, *, topics, age_days, sources, summary="摘要"):
        import json as _json

        now = time.time()
        created = now - age_days * 86400.0
        return mem._redis.set(
            f"ltm:summary:{session_id}",
            _json.dumps(
                {
                    "session_id": session_id,
                    "user_id": "u1",
                    "summary": summary,
                    "topics": list(topics),
                    "key_facts": [],
                    "message_count": 1,
                    "created_at": created,
                    "access_count": 0,
                    "last_accessed": created,
                    "source_message_ids": list(sources),
                }
            ),
        )

    def _memory(self):
        from fakes.redis import FakeRedis  # type: ignore[import-not-found]

        from agentix.application.services.long_term_memory import LongTermMemory

        return LongTermMemory(redis_client=FakeRedis())

    @pytest.mark.asyncio
    async def test_merges_same_topic_within_gap(self):
        mem = self._memory()
        await self._seed(mem, "s1", topics=["租赁"], age_days=5, sources=["m0"])
        await self._seed(mem, "s2", topics=["租赁", "押金"], age_days=4, sources=["m1"])
        await self._seed(mem, "s3", topics=["租赁", "违约金"], age_days=3, sources=["m2"])
        for sid, age in (("s1", 5), ("s2", 4), ("s3", 3)):
            await mem._redis.zadd(
                "ltm:user:u1:sessions", {sid: time.time() - age * 86400.0}
            )

        report = await mem.consolidate("u1")
        assert report["before"] == 3
        assert report["after"] == 1
        assert report["groups"] == 1
        assert report["merged_away"] == 2

    @pytest.mark.asyncio
    async def test_merge_preserves_source_union(self):
        """关键：合并后溯源必须仍是**并集**——否则可重建判据会在整理后失效。"""
        import json as _json

        mem = self._memory()
        await self._seed(mem, "s1", topics=["租赁"], age_days=5, sources=["m0"])
        await self._seed(mem, "s2", topics=["租赁"], age_days=4, sources=["m1"])
        for sid, age in (("s1", 5), ("s2", 4)):
            await mem._redis.zadd(
                "ltm:user:u1:sessions", {sid: time.time() - age * 86400.0}
            )

        await mem.consolidate("u1")
        merged = _json.loads(await mem._redis.get("ltm:summary:s1"))
        assert merged["source_message_ids"] == ["m0", "m1"]
        # 合并后仍可被 is_rebuildable 判定为可重建
        from agentix.application.services.long_term_memory import MemorySummary, is_rebuildable

        assert is_rebuildable([{"id": "m0"}, {"id": "m1"}], MemorySummary.from_dict(merged))

    @pytest.mark.asyncio
    async def test_unrelated_topics_are_not_merged(self):
        mem = self._memory()
        await self._seed(mem, "s1", topics=["租赁"], age_days=3, sources=["m0"])
        await self._seed(mem, "s9", topics=["劳动"], age_days=2, sources=["m9"])
        for sid, age in (("s1", 3), ("s9", 2)):
            await mem._redis.zadd(
                "ltm:user:u1:sessions", {sid: time.time() - age * 86400.0}
            )

        report = await mem.consolidate("u1")
        assert report["groups"] == 0
        assert report["after"] == 2

    @pytest.mark.asyncio
    async def test_dry_run_does_not_write(self):
        """dry-run 必须**零副作用**（整理会改写历史，应先观察再执行）。"""
        mem = self._memory()
        await self._seed(mem, "s1", topics=["租赁"], age_days=5, sources=["m0"])
        await self._seed(mem, "s2", topics=["租赁"], age_days=4, sources=["m1"])
        for sid, age in (("s1", 5), ("s2", 4)):
            await mem._redis.zadd(
                "ltm:user:u1:sessions", {sid: time.time() - age * 86400.0}
            )
        before = sorted(await mem._redis.zrange("ltm:user:u1:sessions", 0, -1))

        report = await mem.consolidate("u1", dry_run=True)
        after = sorted(await mem._redis.zrange("ltm:user:u1:sessions", 0, -1))
        assert report["dry_run"] is True
        assert report["merged_away"] == 1  # 计划算出来了
        assert before == after == ["s1", "s2"]  # 但没写

    @pytest.mark.asyncio
    async def test_empty_user_is_noop(self):
        mem = self._memory()
        report = await mem.consolidate("nobody")
        assert report["before"] == 0
        assert report["after"] == 0

    @pytest.mark.asyncio
    async def test_expired_summaries_are_evicted(self):
        """超过 TTL 且从未被访问的摘要应被淘汰。"""
        mem = self._memory()
        await self._seed(mem, "old", topics=["租赁"], age_days=400, sources=["m0"])
        await mem._redis.zadd("ltm:user:u1:sessions", {"old": time.time() - 400 * 86400.0})

        report = await mem.consolidate("u1", ttl_days=90)
        assert report["evicted"] == 1
        assert report["after"] == 0

    @pytest.mark.asyncio
    async def test_capacity_is_enforced(self):
        mem = self._memory()
        for i in range(5):
            sid = f"s{i}"
            await self._seed(mem, sid, topics=[f"topic{i}"], age_days=1, sources=[f"m{i}"])
            await mem._redis.zadd("ltm:user:u1:sessions", {sid: time.time() - 86400.0})

        report = await mem.consolidate("u1")
        assert report["before"] == 5
        assert report["after"] <= 5
        remaining = await mem._redis.zrange("ltm:user:u1:sessions", 0, -1)
        assert len(remaining) == report["after"]
