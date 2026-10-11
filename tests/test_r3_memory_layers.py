"""R3-8 Semantic Memory 卡片层测试（架构文档 §29-33）。

覆盖：
① extract_cards：偏好句/指示句/普通事实不抽/空输入
② merge_cards：去重（confidence 取 max）/冲突（B1·M-P0-1 **只标记不覆写**：version+1 + conflicts 行）/共存/纯函数不改性
③ SemanticMemoryStore：存取/排序/清空（fake redis）
④ 接线面：LongTermMemory.store_summary 提取卡片 + get_context_for_query 注入
⑤ 四层映射纪律：MEMORY_CARDS_ENABLED 默认开但挂 MEMORY_CONTEXT_ENABLED 之下
"""

from __future__ import annotations

import pytest


# ---------- fake redis（与 test_p26 同风格） ----------

class _FakeRedis:
    def __init__(self):
        self.store: dict = {}
        self.zsets: dict = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def hset(self, key, mapping=None, **kw):
        self.store.setdefault(key, {}).update(mapping or {})

    async def hgetall(self, key):
        return dict(self.store.get(key, {}))

    async def delete(self, key):
        removed = 0
        if self.store.pop(key, None) is not None:
            removed += 1
        if self.zsets.pop(key, None) is not None:
            removed += 1
        return removed

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    async def zremrangebyrank(self, key, start, end):
        return 0

    async def expire(self, key, ttl):
        return True

    async def zrevrange(self, key, start, end):
        items = sorted(self.zsets.get(key, {}).items(), key=lambda kv: -kv[1])
        return [k for k, _ in items[start : end + 1 if end >= 0 else None]]


# ---------- ① extract_cards ----------

def test_extract_cards_preference():
    from agentix.application.services.semantic_memory import (
        CARD_TYPE_PREFERENCE,
        extract_cards,
    )

    summary = "用户要求回答时必须附上法条出处。本次咨询了劳动合同解除相关问题。"
    cards = extract_cards(summary)
    assert len(cards) == 1
    assert cards[0].card_type == CARD_TYPE_PREFERENCE
    assert cards[0].importance >= 0.8
    assert cards[0].confidence > 0


def test_extract_cards_instruction():
    from agentix.application.services.semantic_memory import (
        CARD_TYPE_INSTRUCTION,
        extract_cards,
    )

    # 纯操作性指示（无"用户…希望"偏好引导词——那属于 preference）
    cards = extract_cards("回答要以表格格式呈现对比信息。")
    assert len(cards) == 1
    assert cards[0].card_type == CARD_TYPE_INSTRUCTION


def test_extract_cards_user_wish_is_preference():
    from agentix.application.services.semantic_memory import (
        CARD_TYPE_PREFERENCE,
        extract_cards,
    )

    cards = extract_cards("用户希望回答以表格格式呈现对比信息。")
    assert len(cards) == 1
    assert cards[0].card_type == CARD_TYPE_PREFERENCE


def test_extract_cards_skips_plain_facts():
    from agentix.application.services.semantic_memory import extract_cards

    # 普通事实/无偏好线索 → 不进 Semantic 层（那是 Episodic 的职责）
    assert extract_cards("本次咨询了合同违约金标准。用户任职于某科技公司。") == []


def test_extract_cards_empty():
    from agentix.application.services.semantic_memory import extract_cards

    assert extract_cards("") == []
    assert extract_cards("   \n ") == []


# ---------- ② merge_cards ----------

def _card(content, card_type="preference", **kw):
    from agentix.application.services.semantic_memory import MemoryCard

    return MemoryCard(card_id=kw.pop("card_id", "new1"), card_type=card_type, content=content, **kw)


def test_merge_dedup_bumps_confidence():
    from agentix.application.services.semantic_memory import merge_cards

    old = _card("用户要求回答附法条出处", card_id="old1", confidence=0.6)
    incoming = [_card("用户要求回答附法条出处！", confidence=0.9)]  # 仅标点差异
    merged, report = merge_cards([old], incoming)
    assert report.deduped == 1 and report.added == 0
    assert len(merged) == 1
    assert merged[0].confidence == 0.9  # 取 max
    assert merged[0].card_id == "old1"  # 保留旧卡 id（不新建）


def test_merge_conflict_bumps_version():
    """B1·M-P0-1：同键冲突**只标记不覆写**——原内容保留 + conflicts 行 + version+1。"""
    from agentix.application.services.semantic_memory import merge_cards

    old = _card("用户偏好简洁回答不带出处", card_id="old1", version=2)
    incoming = [_card("用户偏好简洁回答", confidence=0.9)]  # 互为包含 → 冲突
    merged, report = merge_cards([old], incoming)
    assert report.conflicts == 1
    assert len(merged) == 1
    assert merged[0].version == 3  # 修订号+1
    assert merged[0].content == "用户偏好简洁回答不带出处", "原值不得被覆写"
    assert merged[0].card_id == "old1"
    assert len(merged[0].conflicts) == 1, "同键只保留一条冲突行"
    row = merged[0].conflicts[0]
    assert set(row["values"]) == {"用户偏好简洁回答不带出处", "用户偏好简洁回答"}


def test_merge_different_type_coexists():
    from agentix.application.services.semantic_memory import merge_cards

    old = _card("用户偏好简洁回答", card_type="preference", card_id="old1")
    incoming = [_card("用户偏好简洁回答", card_type="instruction", card_id="new1")]
    merged, report = merge_cards([old], incoming)
    assert report.added == 1 and len(merged) == 2


def test_merge_pure_function_does_not_mutate():
    from agentix.application.services.semantic_memory import merge_cards

    old = _card("用户要求回答附法条出处", card_id="old1", confidence=0.6)
    snapshot = old.to_dict()
    merge_cards([old], [_card("用户要求回答附法条出处", confidence=0.9)])
    assert old.to_dict() == snapshot  # 输入对象不被修改


# ---------- ③ SemanticMemoryStore ----------

@pytest.mark.asyncio
async def test_store_and_recall_sorted():
    from agentix.application.services.semantic_memory import (
        SemanticMemoryStore,
        extract_cards,
    )

    store = SemanticMemoryStore(redis_client=_FakeRedis())
    cards = extract_cards(
        "用户要求回答时附法条出处。用户希望以表格格式输出。本次咨询了竞业限制。"
    )
    assert len(cards) == 2
    await store.store_cards("u1", cards)
    recalled = await store.recall_cards("u1")
    assert len(recalled) == 2
    # importance 降序：preference(0.8) 在 instruction(0.7) 前
    assert recalled[0].importance >= recalled[1].importance


@pytest.mark.asyncio
async def test_store_dedup_across_calls():
    from agentix.application.services.semantic_memory import (
        SemanticMemoryStore,
        extract_cards,
    )

    store = SemanticMemoryStore(redis_client=_FakeRedis())
    await store.store_cards("u1", extract_cards("用户要求回答附法条出处，不要遗漏。"))
    report = await store.store_cards("u1", extract_cards("用户要求回答附法条出处，不要遗漏。"))
    assert report.deduped == 1 and report.added == 0
    assert len(await store.recall_cards("u1")) == 1


@pytest.mark.asyncio
async def test_clear_cards():
    from agentix.application.services.semantic_memory import (
        SemanticMemoryStore,
        extract_cards,
    )

    store = SemanticMemoryStore(redis_client=_FakeRedis())
    await store.store_cards("u1", extract_cards("用户要求回答附法条出处，不要遗漏。"))
    assert await store.clear_cards("u1") == 1
    assert await store.recall_cards("u1") == []


# ---------- ④ 接线面（LongTermMemory） ----------

@pytest.mark.asyncio
async def test_ltm_store_summary_extracts_cards(monkeypatch):
    from agentix.application.services import long_term_memory as ltm
    from agentix.application.services.semantic_memory import SemanticMemoryStore
    from agentix.config import settings

    monkeypatch.setattr(settings, "MEMORY_CARDS_ENABLED", True, raising=False)
    redis = _FakeRedis()
    ltm_service = ltm.LongTermMemory(redis_client=redis, summary_ttl=60)

    await ltm_service.store_summary(
        "sess1",
        "用户要求回答时必须附上法条出处，禁止遗漏引用。",
        [{"id": "m1", "role": "user", "content": "q"}],
        user_id="u9",
    )
    # 直接经 store 验证
    store = SemanticMemoryStore(redis_client=redis, prefix="ltm")
    cards = await store.recall_cards("u9")
    assert len(cards) >= 1


@pytest.mark.asyncio
async def test_ltm_context_includes_cards(monkeypatch):
    from agentix.application.services import long_term_memory as ltm
    from agentix.application.services.semantic_memory import SemanticMemoryStore, MemoryCard
    from agentix.config import settings

    monkeypatch.setattr(settings, "MEMORY_CARDS_ENABLED", True, raising=False)
    redis = _FakeRedis()
    ltm_service = ltm.LongTermMemory(redis_client=redis, summary_ttl=60)

    store = SemanticMemoryStore(redis_client=redis, prefix="ltm")
    await store.store_cards(
        "u9",
        [MemoryCard(card_id="c1", card_type="preference", content="回答附法条出处", importance=0.8)],
    )

    context = await ltm_service.get_context_for_query("劳动合同", user_id="u9")
    assert "回答附法条出处" in context
    assert "用户稳定偏好" in context


@pytest.mark.asyncio
async def test_ltm_context_cards_disabled(monkeypatch):
    from agentix.application.services import long_term_memory as ltm
    from agentix.application.services.semantic_memory import SemanticMemoryStore, MemoryCard
    from agentix.config import settings

    monkeypatch.setattr(settings, "MEMORY_CARDS_ENABLED", False, raising=False)
    redis = _FakeRedis()
    ltm_service = ltm.LongTermMemory(redis_client=redis, summary_ttl=60)
    store = SemanticMemoryStore(redis_client=redis, prefix="ltm")
    await store.store_cards(
        "u9",
        [MemoryCard(card_id="c1", card_type="preference", content="回答附法条出处", importance=0.8)],
    )
    # 无摘要无画像 + 卡片被关 → 空上下文（与旧行为一致）
    assert await ltm_service.get_context_for_query("q", user_id="u9") == ""


# ---------- ⑤ 默认值 ----------

def test_settings_defaults():
    from agentix.config import settings

    assert settings.MEMORY_CARDS_ENABLED is True
    assert settings.MEMORY_CONTEXT_ENABLED is False  # 总闸默认关 ⇒ 默认零行为变化
