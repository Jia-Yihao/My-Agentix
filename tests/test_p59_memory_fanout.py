"""P6-6.1 长时记忆（source-code-study §3.5/§5 表 6.1）：MemoryPort 扇出 + Redis LTM 适配器。

钉死契约：

1. **MemoryManager**：边界 fail-fast（空 provider 拒绝 ValueError、非 MemoryPort
   拒绝 TypeError）；``recall`` fail-open **永不抛**（单 provider 超时/异常记
   warning 跳过、全败返回 ``[]``、合并经内容去重 + 冲突裁决 + confidence
   降序稳定排序后截断）；``remember`` scope 不支持 = 跳过（不计失败）、
   ≥1 成功返回首个 id、全败 ``RuntimeError``（honest 不静默丢写）。
2. **LongTermMemoryProvider**：scope 隔离（非 user → ``[]`` / remember 抛
   ``MemoryScopeUnsupported``）、PII 脱敏、确定性 id 幂等覆盖、上限裁剪、
   坏条目 granular fail-open、排序 ``(-confidence, item_id)`` 确定。
3. **chat 接线**：``MEMORY_FANOUT_ENABLED`` 默认关且挂在
   ``MEMORY_CONTEXT_ENABLED`` 通道之下——flag-off 与基线逐位等价且零触达
   manager；flag-on 追加「长期记忆片段」段、扇出失败保 base、base 失败返回 ``""``。
4. **6.4 分段 fail-open**：``get_context_for_query`` 摘要段/画像段各自兜底
   （任一段失败只丢该段，部分存活）。
"""
from __future__ import annotations

import asyncio

import pytest

from agentix.agent.memory.service import InMemoryMemory
from agentix.application.services.memory_manager import (
    MemoryManager,
    MemoryScopeUnsupported,
    LongTermMemoryProvider,
    reset_memory_manager,
)
from agentix.config import settings
from agentix.domain.agent.types import MemoryItem, MemoryKind, MemoryScope

# ---------------------------------------------------------------------------
# 基线断言：flag 默认关（flag 治理；见 configuration.md §5）
# ---------------------------------------------------------------------------

assert settings.MEMORY_FANOUT_ENABLED is False, "P6 基线：记忆扇出默认关"


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


def _item(
    content,
    *,
    conf=1.0,
    kind=MemoryKind.EPISODIC,
    scope=MemoryScope.USER,
    subject="u1",
    tags=(),
    item_id="",
):
    return MemoryItem(
        content=content,
        kind=kind,
        scope=scope,
        item_id=item_id,
        subject=subject,
        confidence=conf,
        tags=tags,
    )


class _StubProvider:
    """可控 provider：items 原样返回 / recall_error 抛错 / delay 模拟超时。"""

    def __init__(self, items=(), *, recall_error=None, remember_error=None, delay=0.0):
        self.items = list(items)
        self.recall_error = recall_error
        self.remember_error = remember_error
        self.delay = delay
        self.remember_calls = 0

    async def recall(self, *, scope, subject, query="", limit=10):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.recall_error is not None:
            raise self.recall_error
        return list(self.items)  # 截断由 MemoryManager 统一负责

    async def remember(self, item):
        self.remember_calls += 1
        if self.remember_error is not None:
            raise self.remember_error
        return f"stub:{item.item_id or item.content[:8]}"


class _ScopeSkipProvider:
    """只声明不支持某 scope 的 provider（remember 抛 MemoryScopeUnsupported）。"""

    async def recall(self, *, scope, subject, query="", limit=10):
        raise MemoryScopeUnsupported("only task scope here")

    async def remember(self, item):
        raise MemoryScopeUnsupported("only task scope here")


class _FakeRedis:
    """hset/hgetall/delete 最小 fake（与 test_r3 同风格）。"""

    def __init__(self):
        self.store = {}

    async def hset(self, key, mapping=None, **kw):
        self.store.setdefault(key, {}).update(mapping or {})

    async def hgetall(self, key):
        return dict(self.store.get(key, {}))

    async def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0


def _adapter(**kw):
    return LongTermMemoryProvider(redis_client=_FakeRedis(), **kw)


# ---------------------------------------------------------------------------
# 1. MemoryManager 边界与 recall fail-open
# ---------------------------------------------------------------------------


class TestManagerBoundary:
    def test_empty_providers_rejected(self):
        with pytest.raises(ValueError):
            MemoryManager([])

    def test_non_port_provider_rejected(self):
        with pytest.raises(TypeError):
            MemoryManager([object()])

    def test_valid_providers_accepted(self):
        manager = MemoryManager([InMemoryMemory(), _StubProvider()])
        assert isinstance(manager, MemoryManager)


class TestManagerRecall:
    @pytest.mark.asyncio
    async def test_all_fail_returns_empty_and_records_degraded(self):
        manager = MemoryManager(
            [_StubProvider(recall_error=RuntimeError("redis down"))],
            timeout_s=0.5,
        )
        assert await manager.recall(scope="user", subject="u1") == []
        assert manager.last_degraded == ("_StubProvider",)

    @pytest.mark.asyncio
    async def test_partial_fail_serves_survivors(self):
        good = _StubProvider([_item("健康条目", conf=0.9)])
        bad = _StubProvider(recall_error=RuntimeError("boom"))
        manager = MemoryManager([bad, good], timeout_s=0.5)
        items = await manager.recall(scope="user", subject="u1")
        assert [it.content for it in items] == ["健康条目"]
        assert manager.last_degraded == ("_StubProvider",)

    @pytest.mark.asyncio
    async def test_slow_provider_times_out_without_hanging(self):
        slow = _StubProvider([_item("慢")], delay=5.0)
        fast = _StubProvider([_item("快", conf=0.8)])
        manager = MemoryManager([slow, fast], timeout_s=0.05)
        items = await manager.recall(scope="user", subject="u1")
        assert [it.content for it in items] == ["快"]
        assert manager.last_degraded == ("_StubProvider",)

    @pytest.mark.asyncio
    async def test_dedup_same_content_keeps_higher_confidence(self):
        a = _StubProvider([_item("同内容", conf=0.4)])
        b = _StubProvider([_item("同内容", conf=0.9)])
        manager = MemoryManager([a, b], timeout_s=0.5)
        items = await manager.recall(scope="user", subject="u1")
        assert len(items) == 1
        assert items[0].confidence == 0.9

    @pytest.mark.asyncio
    async def test_conflict_resolution_keeps_highest_confidence(self):
        # 同 (kind, subject, tags[0])、不同内容 → resolve_conflicts 裁决
        a = _StubProvider([_item("旧说法", conf=0.5, tags=("t",))])
        b = _StubProvider([_item("新说法", conf=0.9, tags=("t",))])
        manager = MemoryManager([a, b], timeout_s=0.5)
        items = await manager.recall(scope="user", subject="u1")
        assert [it.content for it in items] == ["新说法"]

    @pytest.mark.asyncio
    async def test_confidence_desc_order_and_limit(self):
        provider = _StubProvider(
            [
                _item("低", conf=0.5, tags=("a",)),
                _item("高", conf=0.9, tags=("b",)),
                _item("中", conf=0.7, tags=("c",)),
            ]
        )
        manager = MemoryManager([provider], timeout_s=0.5)
        items = await manager.recall(scope="user", subject="u1", limit=2)
        assert [it.content for it in items] == ["高", "中"]

    @pytest.mark.asyncio
    async def test_recall_never_raises_when_everything_broken(self):
        class _Explosive:
            async def recall(self, **kw):
                raise MemoryError("灾难")

            async def remember(self, item):
                raise MemoryError("灾难")

        manager = MemoryManager([_Explosive()], timeout_s=0.5)
        assert await manager.recall(scope="user", subject="u1") == []


# ---------------------------------------------------------------------------
# 2. MemoryManager remember 语义
# ---------------------------------------------------------------------------


class TestManagerRemember:
    @pytest.mark.asyncio
    async def test_returns_first_success_id(self):
        a = _StubProvider()
        b = _StubProvider()
        manager = MemoryManager([a, b], timeout_s=0.5)
        assert await manager.remember(_item("写入", item_id="x1")) == "stub:x1"

    @pytest.mark.asyncio
    async def test_scope_unsupported_provider_is_skipped_not_failed(self):
        skipper = _ScopeSkipProvider()
        keeper = InMemoryMemory()
        manager = MemoryManager([skipper, keeper], timeout_s=0.5)
        item_id = await manager.remember(
            _item("任务记忆", scope=MemoryScope.TASK, subject="t1")
        )
        assert isinstance(item_id, str) and item_id  # InMemoryMemory 成功即可

    @pytest.mark.asyncio
    async def test_partial_failure_returns_success(self):
        bad = _StubProvider(remember_error=RuntimeError("redis down"))
        good = _StubProvider()
        manager = MemoryManager([bad, good], timeout_s=0.5)
        assert await manager.remember(_item("写入", item_id="k")) == "stub:k"

    @pytest.mark.asyncio
    async def test_all_fail_raises_runtime_error(self):
        bad1 = _StubProvider(remember_error=RuntimeError("down-1"))
        bad2 = _StubProvider(remember_error=RuntimeError("down-2"))
        manager = MemoryManager([bad1, bad2], timeout_s=0.5)
        with pytest.raises(RuntimeError, match="no memory provider accepted"):
            await manager.remember(_item("写入"))

    @pytest.mark.asyncio
    async def test_all_scope_unsupported_raises(self):
        manager = MemoryManager([_ScopeSkipProvider(), _ScopeSkipProvider()], timeout_s=0.5)
        with pytest.raises(RuntimeError, match="no memory provider supports"):
            await manager.remember(_item("写入"))


# ---------------------------------------------------------------------------
# 3. LongTermMemoryProvider（Redis LTM 适配器）
# ---------------------------------------------------------------------------


class TestAdapter:
    @pytest.mark.asyncio
    async def test_recall_scope_isolation(self):
        adapter = _adapter()
        assert await adapter.recall(scope="knowledge", subject="u1") == []
        assert await adapter.recall(scope="task", subject="u1") == []

    @pytest.mark.asyncio
    async def test_remember_rejects_non_user_scope(self):
        adapter = _adapter()
        with pytest.raises(MemoryScopeUnsupported):
            await adapter.remember(_item("x", scope=MemoryScope.TASK, subject="t1"))

    @pytest.mark.asyncio
    async def test_remember_requires_subject(self):
        adapter = _adapter()
        with pytest.raises(ValueError):
            await adapter.remember(_item("x", subject=""))

    @pytest.mark.asyncio
    async def test_remember_redacts_pii(self):
        adapter = _adapter()
        await adapter.remember(_item("联系电话 13800138000", subject="u1"))
        raw = adapter._client().store[f"ltm:port:u1"]
        blob = str(raw)
        assert "13800138000" not in blob
        assert "[PHONE]" in blob

    @pytest.mark.asyncio
    async def test_remember_deterministic_id_is_idempotent(self):
        adapter = _adapter()
        id1 = await adapter.remember(_item("同一内容", subject="u1"))
        id2 = await adapter.remember(_item("同一内容", subject="u1"))
        assert id1 == id2
        raw = await adapter._client().hgetall("ltm:port:u1")
        assert len(raw) == 1  # 幂等覆盖，不重复

    @pytest.mark.asyncio
    async def test_roundtrip_preserves_kind_confidence_tags(self):
        adapter = _adapter()
        item_id = await adapter.remember(
            _item(
                "偏好表格呈现",
                conf=0.7,
                kind=MemoryKind.USER,
                tags=("preference", "表格"),
                subject="u1",
            )
        )
        items = await adapter.recall(scope="user", subject="u1")
        assert len(items) == 1
        got = items[0]
        assert got.item_id == item_id
        assert got.kind == MemoryKind.USER
        assert got.scope == MemoryScope.USER
        assert got.confidence == 0.7
        assert got.tags == ("preference", "表格")

    @pytest.mark.asyncio
    async def test_recall_keyword_filters_content_and_tags(self):
        adapter = _adapter()
        await adapter.remember(_item("回答用表格呈现", subject="u1", tags=("表格",)))
        assert len(await adapter.recall(scope="user", subject="u1", query="表格")) == 1
        assert await adapter.recall(scope="user", subject="u1", query="无关词") == []

    @pytest.mark.asyncio
    async def test_bad_json_entry_skipped_others_served(self):
        adapter = _adapter()
        good_id = await adapter.remember(_item("好条目", subject="u1"))
        key = "ltm:port:u1"
        adapter._client().store[key]["corrupt"] = "{not-json"
        items = await adapter.recall(scope="user", subject="u1")
        assert [it.item_id for it in items] == [good_id]

    @pytest.mark.asyncio
    async def test_recall_sort_deterministic(self):
        adapter = _adapter()
        await adapter.remember(_item("低", conf=0.5, subject="u1", item_id="b"))
        await adapter.remember(_item("高", conf=0.9, subject="u1", item_id="a"))
        await adapter.remember(_item("并列", conf=0.9, subject="u1", item_id="c"))
        items = await adapter.recall(scope="user", subject="u1")
        # (-confidence, item_id)：0.9 档内按 id 升序 → a, c；再 0.5 → b
        assert [it.item_id for it in items] == ["a", "c", "b"]

    @pytest.mark.asyncio
    async def test_max_items_cap_trims_oldest(self):
        adapter = _adapter(max_items=2)
        await adapter.remember(_item("一", subject="u1"))
        await adapter.remember(_item("二", subject="u1"))
        last_id = await adapter.remember(_item("三", subject="u1"))
        raw = await adapter._client().hgetall("ltm:port:u1")
        assert len(raw) == 2
        assert last_id in raw  # 新者存活


# ---------------------------------------------------------------------------
# 4. 单例
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_get_returns_same_instance(self):
        try:
            m1 = MemoryManager([InMemoryMemory()])  # 仅确保构造可用
            assert isinstance(m1, MemoryManager)
            from agentix.application.services import memory_manager as mm

            mm.reset_memory_manager()
            a = mm.get_memory_manager()
            b = mm.get_memory_manager()
            assert a is b
        finally:
            reset_memory_manager()

    def test_reset_rebuilds(self):
        from agentix.application.services import memory_manager as mm

        try:
            first = mm.get_memory_manager()
            mm.reset_memory_manager()
            second = mm.get_memory_manager()
            assert first is not second
        finally:
            mm.reset_memory_manager()


# ---------------------------------------------------------------------------
# 5. chat 接线（flag 两态）
# ---------------------------------------------------------------------------


class _SpyManager:
    def __init__(self, items=(), error=None):
        self.items = list(items)
        self.error = error
        self.calls = 0

    async def recall(self, **kw):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.items)


class _FakeLTM:
    def __init__(self, base):
        self.base = base

    async def get_context_for_query(self, query, *, max_summaries=3):
        if isinstance(self.base, Exception):
            raise self.base
        return self.base


class TestChatFanoutWiring:
    BASE = "相关历史对话摘要：\n  1. 旧摘要"

    def _patch_common(self, monkeypatch, *, fanout):
        from agentix.application.services import long_term_memory as ltm

        monkeypatch.setattr(settings, "MEMORY_CONTEXT_ENABLED", True)
        monkeypatch.setattr(settings, "MEMORY_RECALL_LIMIT", 3)
        monkeypatch.setattr(settings, "MEMORY_FANOUT_ENABLED", fanout)
        monkeypatch.setattr(ltm, "get_long_term_memory", lambda: _FakeLTM(self.BASE))

    @pytest.mark.asyncio
    async def test_flag_off_returns_base_and_never_touches_manager(self, monkeypatch):
        from agentix.application.services import chat_service
        from agentix.application.services import memory_manager as mm

        spy = _SpyManager(items=[_item("不该出现")])
        monkeypatch.setattr(mm, "_manager", spy)
        self._patch_common(monkeypatch, fanout=False)
        result = await chat_service._maybe_memory_context("咨询劳动合同")
        assert result == self.BASE  # 与基线逐位等价
        assert spy.calls == 0  # 零触达

    @pytest.mark.asyncio
    async def test_flag_on_appends_fanout_section(self, monkeypatch):
        from agentix.application.services import chat_service
        from agentix.application.services import memory_manager as mm

        item = _item(
            "偏好表格呈现",
            kind=MemoryKind.USER,
            tags=("preference",),
            conf=0.9,
        )
        monkeypatch.setattr(mm, "_manager", _SpyManager(items=[item]))
        self._patch_common(monkeypatch, fanout=True)
        result = await chat_service._maybe_memory_context("咨询劳动合同")
        assert result.startswith(self.BASE)
        assert "长期记忆片段（扇出召回）：" in result
        assert "1. 偏好表格呈现 [preference]" in result

    @pytest.mark.asyncio
    async def test_flag_on_manager_failure_preserves_base(self, monkeypatch):
        from agentix.application.services import chat_service
        from agentix.application.services import memory_manager as mm

        monkeypatch.setattr(
            mm, "_manager", _SpyManager(error=RuntimeError("redis down"))
        )
        self._patch_common(monkeypatch, fanout=True)
        result = await chat_service._maybe_memory_context("咨询劳动合同")
        assert result == self.BASE  # 扇出失败不丢既有上下文

    @pytest.mark.asyncio
    async def test_flag_on_empty_items_returns_base_unchanged(self, monkeypatch):
        from agentix.application.services import chat_service
        from agentix.application.services import memory_manager as mm

        monkeypatch.setattr(mm, "_manager", _SpyManager(items=[]))
        self._patch_common(monkeypatch, fanout=True)
        result = await chat_service._maybe_memory_context("咨询劳动合同")
        assert result == self.BASE  # 空结果不追加空行

    @pytest.mark.asyncio
    async def test_base_failure_returns_empty_even_with_fanout_on(self, monkeypatch):
        from agentix.application.services import chat_service
        from agentix.application.services import long_term_memory as ltm
        from agentix.application.services import memory_manager as mm

        monkeypatch.setattr(mm, "_manager", _SpyManager(items=[_item("x")]))
        monkeypatch.setattr(
            ltm, "get_long_term_memory", lambda: _FakeLTM(RuntimeError("down"))
        )
        monkeypatch.setattr(settings, "MEMORY_CONTEXT_ENABLED", True)
        monkeypatch.setattr(settings, "MEMORY_FANOUT_ENABLED", True)
        result = await chat_service._maybe_memory_context("咨询劳动合同")
        assert result == ""

    @pytest.mark.asyncio
    async def test_fanout_nested_under_context_gate(self, monkeypatch):
        from agentix.application.services import chat_service
        from agentix.application.services import memory_manager as mm

        spy = _SpyManager(items=[_item("不该出现")])
        monkeypatch.setattr(mm, "_manager", spy)
        monkeypatch.setattr(settings, "MEMORY_CONTEXT_ENABLED", False)
        monkeypatch.setattr(settings, "MEMORY_FANOUT_ENABLED", True)
        result = await chat_service._maybe_memory_context("咨询劳动合同")
        assert result == ""
        assert spy.calls == 0


# ---------------------------------------------------------------------------
# 6. 6.4 分段 fail-open（get_context_for_query）
# ---------------------------------------------------------------------------


class TestContextSegmentFailOpen:
    def _ltm(self):
        from agentix.application.services.long_term_memory import LongTermMemory

        return LongTermMemory(redis_client=_FakeRedis())

    @pytest.mark.asyncio
    async def test_summary_failure_keeps_profile(self, monkeypatch):
        from agentix.application.services.long_term_memory import UserProfile

        ltm = self._ltm()

        async def _boom(*a, **k):
            raise RuntimeError("recall down")

        async def _profile(user_id):
            return UserProfile(
                user_id=user_id, total_sessions=3, frequent_topics=["劳动法"]
            )

        monkeypatch.setattr(ltm, "recall", _boom)
        monkeypatch.setattr(ltm, "get_user_profile", _profile)
        out = await ltm.get_context_for_query("咨询")
        assert "共 3 次会话" in out
        assert "劳动法" in out

    @pytest.mark.asyncio
    async def test_profile_failure_keeps_summaries(self, monkeypatch):
        from agentix.application.services.long_term_memory import MemorySummary

        ltm = self._ltm()

        async def _recall(user_id, limit=3, query=""):
            return [
                MemorySummary(
                    session_id="s1", user_id=user_id, summary="咨询过劳动合同"
                )
            ]

        async def _boom(*a, **k):
            raise RuntimeError("profile down")

        monkeypatch.setattr(ltm, "recall", _recall)
        monkeypatch.setattr(ltm, "get_user_profile", _boom)
        out = await ltm.get_context_for_query("咨询")
        assert "咨询过劳动合同" in out

    @pytest.mark.asyncio
    async def test_both_fail_returns_cards_or_empty(self, monkeypatch):
        ltm = self._ltm()

        async def _boom(*a, **k):
            raise RuntimeError("down")

        monkeypatch.setattr(ltm, "recall", _boom)
        monkeypatch.setattr(ltm, "get_user_profile", _boom)
        out = await ltm.get_context_for_query("咨询")
        # 卡片区 fail-open 返回空（fake redis 无数据）→ 空串而非抛错
        assert out == ""

    @pytest.mark.asyncio
    async def test_healthy_segments_all_present(self, monkeypatch):
        from agentix.application.services.long_term_memory import (
            MemorySummary,
            UserProfile,
        )

        ltm = self._ltm()

        async def _recall(user_id, limit=3, query=""):
            return [
                MemorySummary(
                    session_id="s1", user_id=user_id, summary="咨询过劳动合同"
                )
            ]

        async def _profile(user_id):
            return UserProfile(
                user_id=user_id, total_sessions=5, frequent_topics=["劳动法"]
            )

        monkeypatch.setattr(ltm, "recall", _recall)
        monkeypatch.setattr(ltm, "get_user_profile", _profile)
        out = await ltm.get_context_for_query("咨询")
        assert "共 5 次会话" in out
        assert "咨询过劳动合同" in out
