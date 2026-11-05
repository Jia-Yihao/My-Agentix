"""B1 · 32-P0 记忆收敛守卫（M-P0-1/3/4/5，v2 Stage B1 → p67）。

覆盖（验收）：

- ① 序列化只增：双卡 schema 版本单源、旧 dict 读入给默认、to_dict 只增键、
  冲突行 shape 统一 ``{"key","values"}``；
- ② merge 只标记不覆写：原内容保留 + conflicts 行累积 + version+1 + 纯函数；
- ③ 脱敏补齐：基线四规则只增不减、中英姓名/地址、legal 文本零误伤、幂等、
  写路径零留存（InMemoryMemory / store_summary / store_cards）、
  读路径兜底（legacy 直存原文 + 冲突标记行）；
- ④ 冲突键加固：全 tags 键、时间窗分簇、窗内标记不静默、窗外不裁决、
  缺 stored_at 兼容、平分确定性、纯函数不改输入；
- ⑤ scope 计数与文档一致（3 scope，M-P0-5）；
- ⑥ evals 报告键（离线确定性，privacy_leak 必须 0）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentix.agent.memory import InMemoryMemory, redact_pii, resolve_conflicts
from agentix.agent.memory.service import CONFLICT_TIME_WINDOW_DAYS
from agentix.application.services.card_schema import (
    CARD_SCHEMA_VERSION,
    upsert_conflict_row,
)
from agentix.application.services.long_term_memory import (
    PROFILE_CARD_SCHEMA_VERSION,
    fold_profile_card,
)
from agentix.application.services.semantic_memory import (
    MemoryCard,
    merge_cards,
)
from agentix.domain.agent.types import MemoryItem, MemoryKind, MemoryScope

_ROOT = Path(__file__).resolve().parents[1]


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


def _mem(content, *, confidence=0.5, tags=(), stored_at=None, subject="u1"):
    meta = {} if stored_at is None else {"stored_at": stored_at}
    return MemoryItem(
        content=content,
        kind=MemoryKind.USER,
        scope=MemoryScope.USER,
        subject=subject,
        confidence=confidence,
        tags=tags,
        meta=meta,
    )


_TS = 1_700_000_000.0


# ===========================================================================
# ① 序列化只增（统一卡片 schema）
# ===========================================================================

def test_card_schema_version_is_single_source():
    """M-P0-1：画像卡与语义卡 schema 版本同源（card_schema.CARD_SCHEMA_VERSION）。"""
    assert PROFILE_CARD_SCHEMA_VERSION == CARD_SCHEMA_VERSION
    assert MemoryCard(card_id="c", card_type="preference", content="x").schema_version == CARD_SCHEMA_VERSION


def test_memory_card_from_legacy_dict_defaults():
    """旧序列化（无新键）读入 → 新字段取默认，不猜不迁移。"""
    legacy = {"card_id": "c1", "card_type": "preference", "content": "旧卡"}
    card = MemoryCard.from_dict(legacy)
    assert card.content == "旧卡"
    assert card.schema_version == CARD_SCHEMA_VERSION
    assert card.conflicts == []


def test_memory_card_to_dict_is_superset_of_legacy():
    """to_dict 只增键：旧九键一个不少地保留。"""
    legacy_keys = {
        "card_id", "card_type", "content", "importance", "confidence",
        "version", "source", "created_at", "updated_at",
    }
    data = MemoryCard(card_id="c", card_type="preference", content="x").to_dict()
    assert legacy_keys <= set(data)
    assert data["schema_version"] == CARD_SCHEMA_VERSION


def test_conflict_row_shape_unified_across_both_cards():
    """双卡冲突行 shape 统一为 {"key","values"}（fold 与 upsert helper 同构）。"""
    card = fold_profile_card(
        None, {"created_at": _TS, "preferences": {"风格": "简洁"}}
    )
    card = fold_profile_card(card, {"created_at": _TS + 1, "preferences": {"风格": "详尽"}})
    assert [set(row) for row in card["conflicts"]] == [{"key", "values"}]

    rows = upsert_conflict_row([], "风格", ["简洁", "详尽"])
    assert [set(row) for row in rows] == [{"key", "values"}]

    old = MemoryCard(card_id="a", card_type="preference", content="用户偏好简洁回答")
    merged, _ = merge_cards(
        [old],
        [MemoryCard(card_id="b", card_type="preference", content="用户偏好简洁回答不带出处")],
    )
    assert [set(row) for row in merged[0].conflicts] == [{"key", "values"}]


def test_upsert_conflict_row_is_pure_and_dedups():
    """helper 纯函数：不改输入、同键同行、值去重排序与次序无关。"""
    base = [{"key": "k", "values": ["a"]}]
    snapshot = json.loads(json.dumps(base))
    out1 = upsert_conflict_row(base, "k", ["b", "a"])
    out2 = upsert_conflict_row(base, "k", ["b", "a"])
    assert base == snapshot, "输入不得被修改"
    assert out1 == out2
    assert out1[0]["values"] == sorted(out1[0]["values"], key=lambda v: json.dumps(v, sort_keys=True, ensure_ascii=False))
    out3 = upsert_conflict_row(out1, "z", ["v"])
    assert [row["key"] for row in out3] == ["k", "z"]


# ===========================================================================
# ② merge 只标记不覆写
# ===========================================================================

def test_merge_conflict_marks_not_overwrites():
    old = MemoryCard(card_id="old1", card_type="preference", content="用户偏好简洁回答")
    incoming = [MemoryCard(card_id="n1", card_type="preference", content="用户偏好简洁回答不带出处")]
    merged, report = merge_cards([old], incoming)
    assert report.conflicts == 1
    card = merged[0]
    assert card.content == "用户偏好简洁回答", "原值不得被覆写"
    assert card.version == 2 and card.card_id == "old1"
    assert len(card.conflicts) == 1
    assert set(card.conflicts[0]["values"]) == {"用户偏好简洁回答", "用户偏好简洁回答不带出处"}


def test_merge_conflict_accumulates_into_same_row():
    """二次冲突：同键仍只一行，values 累积去重。"""
    old = MemoryCard(card_id="old1", card_type="preference", content="用户偏好简洁回答")
    incoming = [
        MemoryCard(card_id="n1", card_type="preference", content="用户偏好简洁回答不带出处"),
        MemoryCard(card_id="n2", card_type="preference", content="偏好简洁"),
    ]
    merged, report = merge_cards([old], incoming)
    assert report.conflicts == 2
    assert len(merged[0].conflicts) == 1, "同键只保留一条冲突行"
    values = merged[0].conflicts[0]["values"]
    assert len(values) == 3
    assert set(values) == {"用户偏好简洁回答", "用户偏好简洁回答不带出处", "偏好简洁"}
    assert merged[0].content == "用户偏好简洁回答"


def test_merge_pure_function_snapshot():
    old = MemoryCard(card_id="old1", card_type="preference", content="用户偏好简洁回答")
    snapshot = old.to_dict()
    merge_cards([old], [MemoryCard(card_id="n1", card_type="preference", content="用户偏好简洁回答哦")])
    assert old.to_dict() == snapshot


# ===========================================================================
# ③ 脱敏补齐（M-P0-3）
# ===========================================================================

def test_redact_baseline_four_rules_unchanged():
    """基线四规则只增不减：PHONE/EMAIL/ID/CARD 仍逐条命中。"""
    out = redact_pii("联系 13812345678 或 a@b.com，身份证 110101199001011234，卡 4111 1111 1111 1111")
    assert "13812345678" not in out and "[PHONE]" in out
    assert "a@b.com" not in out and "[EMAIL]" in out
    assert "110101199001011234" not in out and "[ID]" in out
    assert "4111 1111 1111 1111" not in out and "[CARD]" in out


def test_redact_chinese_name_and_address():
    cases = {
        "我叫张三，请联系我": "[NAME]",
        "姓名：李四": "[NAME]",
        "住北京市海淀区中关村大街27号": "[ADDR]",
        "现住址 科技园12栋305室": "[ADDR]",
        "收件地址：阳光花园5栋2单元801室": "[ADDR]",
    }
    for raw, token in cases.items():
        out = redact_pii(raw)
        assert token in out, f"{raw!r} 未命中 {token}: {out!r}"
        assert raw not in out


def test_redact_english_name_and_address():
    cases = {
        "my name is John Smith": "[NAME]",
        "I'm Alice": "[NAME]",
        "I am Bob Lee": "[NAME]",
        "ship to 123 Main Street": "[ADDR]",
        "at 45 Oak Avenue, please": "[ADDR]",
    }
    for raw, token in cases.items():
        out = redact_pii(raw)
        assert token in out, f"{raw!r} 未命中 {token}: {out!r}"
        assert raw not in out


def test_redact_no_false_positive_on_legal_text():
    """锚点式规则：legal/英文正文零误伤。"""
    safe = [
        "联系人要求附法条出处，不得遗漏引用",  # 「联系人」后无冒号不命中姓名规则
        "该条款与地址无关，姓名与名称不同",    # 「姓名」后无冒号不命中
        "I am reading the contract",          # 小写不命中大小写敏感规则
        "诉讼请求要求继续履行合同",            # 常规 legal 正文零命中
    ]
    for raw in safe:
        assert redact_pii(raw) == raw


def test_redact_pii_is_idempotent():
    fixtures = [
        "联系 13812345678",
        "我叫张三",
        "住中关村大街27号",
        "my name is John Smith",
        "123 Main Street",
    ]
    for raw in fixtures:
        once = redact_pii(raw)
        assert redact_pii(once) == once


@pytest.mark.asyncio
async def test_inmemory_write_path_zero_pii():
    mem = InMemoryMemory()
    raws = ["手机 13812345678", "邮箱 a@b.com", "我叫张三"]
    for raw in raws:
        await mem.remember(
            MemoryItem(content=raw, kind=MemoryKind.USER, scope=MemoryScope.USER, subject="u1")
        )
    for raw in raws:
        assert not any(raw in it.content for it in mem.all_items()), f"写路径残留原文: {raw}"


@pytest.mark.asyncio
async def test_inmemory_read_path_covers_legacy_raw():
    """legacy 直存原文（绕过 remember）→ 读路径二次脱敏兜底。"""
    mem = InMemoryMemory()
    mem._items.append(
        _mem("手机号 13812345678", confidence=0.9)
    )
    hits = await mem.recall(scope="user", subject="u1")
    assert len(hits) == 1
    assert "13812345678" not in hits[0].content and "[PHONE]" in hits[0].content


@pytest.mark.asyncio
async def test_inmemory_read_path_redacts_conflict_marker_rows():
    """冲突标记行（meta.conflicts）出库前同样脱敏。"""
    mem = InMemoryMemory()
    mem._items.append(
        MemoryItem(
            content="干净正文",
            kind=MemoryKind.USER,
            scope=MemoryScope.USER,
            subject="u1",
            confidence=0.9,
            meta={"conflicts": [{"item_id": "x", "content": "手机 13900000000"}]},
        )
    )
    hits = await mem.recall(scope="user", subject="u1")
    rows = hits[0].meta.get("conflicts") or []
    assert rows and "13900000000" not in rows[0]["content"]
    assert "[PHONE]" in rows[0]["content"]


@pytest.mark.asyncio
async def test_store_summary_write_path_redacts():
    from agentix.application.services.long_term_memory import LongTermMemory

    redis = _FakeRedis()
    ltm = LongTermMemory(redis_client=redis, summary_ttl=60)
    await ltm.store_summary(
        "sess1",
        "客户手机 13812345678，住中关村大街27号，需回电。",
        messages=[{"id": "m1", "content": "原始对话"}],
        user_id="u1",
        topics=["我叫张三的咨询"],
        key_facts=["联系 a@b.com"],
    )
    stored = json.loads(redis.store["ltm:summary:sess1"])
    blob = json.dumps(stored, ensure_ascii=False)
    for raw in ("13812345678", "中关村大街27号", "我叫张三", "a@b.com"):
        assert raw not in blob, f"store_summary 残留原文: {raw}"
    assert "[PHONE]" in stored["summary"]


@pytest.mark.asyncio
async def test_store_cards_write_path_redacts_and_cleans_legacy():
    """写路径覆盖生产者与直调方；legacy 原文卡重写即清洗。"""
    from agentix.application.services.semantic_memory import SemanticMemoryStore

    redis = _FakeRedis()
    # 预置 legacy 原文卡（绕过脱敏直写 Redis）
    legacy = MemoryCard(card_id="legacy1", card_type="preference", content="手机 13700000000 要优先回电")
    import json as _json
    from agentix.application.services.semantic_memory import _dumps
    redis.store["ltm:cards:u1"] = {"legacy1": _dumps(legacy)}

    store = SemanticMemoryStore(redis_client=redis, prefix="ltm")
    # 新卡入库（触发存量读取 + 重写）
    await store.store_cards("u1", [
        MemoryCard(card_id="n1", card_type="instruction", content="回答要附出处"),
    ])
    cards = await store.recall_cards("u1", limit=10)
    blob = _json.dumps([c.to_dict() for c in cards], ensure_ascii=False)
    assert "13700000000" not in blob, "legacy 原文卡必须在重写中被清洗"
    assert "[PHONE]" in blob


@pytest.mark.asyncio
async def test_get_context_read_path_redacts():
    from agentix.application.services.long_term_memory import (
        LongTermMemory,
        MemorySummary,
    )

    redis = _FakeRedis()
    mem = MemorySummary(
        session_id="sess1",
        user_id="u1",
        summary="客户手机 13812345678 需回电。",
        topics=["合同"],
    )
    redis.store["ltm:summary:sess1"] = json.dumps(mem.to_dict())
    redis.zsets["ltm:user:u1:sessions"] = {"sess1": mem.created_at}
    ltm = LongTermMemory(redis_client=redis, summary_ttl=60)
    ctx = await ltm.get_context_for_query("手机", user_id="u1")
    assert "13812345678" not in ctx, "读路径必须二次脱敏"
    assert "[PHONE]" in ctx


# ===========================================================================
# ④ 冲突键加固（M-P0-4）
# ===========================================================================

def test_resolve_uses_full_tag_set_not_first_tag():
    out = resolve_conflicts(
        [_mem("甲", confidence=0.5, tags=("法律", "合同")),
         _mem("乙", confidence=0.9, tags=("法律", "侵权"))]
    )
    assert len(out) == 2, "全 tags 不同 = 不同事实，不裁决"


def test_resolve_tag_order_insensitive():
    out = resolve_conflicts(
        [_mem("丙", confidence=0.5, tags=("合同", "法律")),
         _mem("丁", confidence=0.9, tags=("法律", "合同"))]
    )
    assert len(out) == 1 and out[0].content == "丁"


def test_resolve_time_window_in_marks_loser_without_mutating_input():
    a = _mem("旧说法", confidence=0.4, tags=("preference",), stored_at=_TS)
    b = _mem("新说法", confidence=0.9, tags=("preference",), stored_at=_TS - 86400.0)
    out = resolve_conflicts([a, b])
    assert len(out) == 1
    winner = out[0]
    assert winner.content == "新说法"
    rows = winner.meta.get("conflicts") or []
    assert len(rows) == 1 and rows[0]["content"] == "旧说法"
    assert rows[0]["confidence"] == 0.4 and rows[0]["stored_at"] == _TS
    assert "conflicts" not in a.meta and "conflicts" not in b.meta, "纯函数不得改输入"


def test_resolve_time_window_out_keeps_both():
    out = resolve_conflicts(
        [_mem("现值", confidence=0.4, tags=("preference",), stored_at=_TS),
         _mem("历史值", confidence=0.9, tags=("preference",),
              stored_at=_TS - (CONFLICT_TIME_WINDOW_DAYS + 5) * 86400.0)]
    )
    assert len(out) == 2, "窗外 = 独立簇各自保留（新旧不互相覆盖）"
    assert all(not it.meta.get("conflicts") for it in out)


def test_resolve_missing_stored_at_joins_current_window():
    """缺 stored_at 视为当下：与存量无时间戳数据兼容（裁决仍发生）。"""
    out = resolve_conflicts(
        [_mem("无时间戳旧值", confidence=0.4, tags=("k",)),
         _mem("有时间戳新值", confidence=0.9, tags=("k",), stored_at=_TS)]
    )
    assert len(out) == 1 and out[0].content == "有时间戳新值"


def test_resolve_tie_keeps_first_seen_deterministic():
    items = [_mem("先见", confidence=0.5, tags=("k",)), _mem("后见", confidence=0.5, tags=("k",))]
    out = resolve_conflicts(items)
    assert [it.content for it in out] == ["先见"]
    assert resolve_conflicts(items) == out, "两次调用结果一致"


def test_resolve_preserves_first_seen_group_order():
    out = resolve_conflicts(
        [_mem("第一组", confidence=0.5, tags=("a",)),
         _mem("第二组", confidence=0.7, tags=("b",)),
         _mem("第三组", confidence=0.6, tags=("c",))]
    )
    assert [it.content for it in out] == ["第一组", "第二组", "第三组"]


# ===========================================================================
# ⑤ scope 计数与文档一致（M-P0-5）
# ===========================================================================

def test_memory_scope_count_is_three():
    assert [s.value for s in MemoryScope] == ["user", "knowledge", "task"]


def test_service_docstring_declares_three_scope():
    text = (_ROOT / "agentix" / "agent" / "memory" / "service.py").read_text(
        encoding="utf-8"
    )
    doc = text.split('"""')[1]
    assert "三 scope" in doc
    assert "两 scope" not in doc


def test_readme_declares_three_scope():
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    assert "3 scope" in readme


# ===========================================================================
# ⑥ evals 报告键（离线确定性）
# ===========================================================================

@pytest.mark.asyncio
async def test_memory_eval_report_keys_and_pass():
    from evals.memory_eval import build_memory_report

    report = await build_memory_report()
    assert set(report) == {"profile_stability", "conflict_precision", "privacy_leak"}
    assert report["profile_stability"] == 1.0, "增量 ≡ 重放 ≡ 乱序重放"
    assert report["conflict_precision"] == 1.0, "该标记的标记、不该标记的不误报"
    assert report["privacy_leak"] == 0, "写路径零 PII 留存"
