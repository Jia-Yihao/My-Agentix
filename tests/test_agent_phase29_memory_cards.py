"""Phase 29（Ch3 §3.5/§3.6 记忆结构化二期：JSON Cards + User as Code）落地测试。

覆盖面（对齐 docs/migration/Phase28-31_优化规划.md Phase 29 DoD）：

1. flag-off：`UserProfile.to_dict()` 序列化与基线字节等价（7 键精确相等，
   即使存储里已有 profile_card 也不写盘）；prompt 拼装无卡片区段。
2. flag-on：卡片写入/读回 roundtrip；旧数据（无 card）读侧正常回退。
3. 派生幂等：增量折入（store_summary→_update_user_profile）结果 ≡
   `derive_profile` 重放同一事件日志；重放两次结果一致（确定性）。
4. 约束冲突被**标记而非覆写**（原值保留 + conflicts 只增）。
5. 触发器只在 memory_consolidation（flag 开、非 dry_run）重派生；
   dry_run/空日志零写入；淘汰仍走 Phase 24 归档红线。
6. 标签双轨：卡片轨（profile_card）新增不删笔记轨（摘要/Simple Notes）。

全程离线：FakeRedis + 纯函数，零外部依赖。
"""

import json
import time

import pytest

from agentix.application.services import long_term_memory as ltm_module
from agentix.application.services.long_term_memory import (
    PROFILE_CARD_SCHEMA_VERSION,
    LongTermMemory,
    UserProfile,
    derive_profile,
    fold_profile_card,
)
from agentix.application.services.task_handlers import handle_memory_consolidation
from agentix.config import settings as app_settings
from fakes.redis import FakeRedis  # type: ignore[import-not-found]

DAY = 86400.0
BASELINE_KEYS = {
    "user_id",
    "total_sessions",
    "total_messages",
    "frequent_topics",
    "preferences",
    "last_active",
    "created_at",
}


def _memory() -> LongTermMemory:
    return LongTermMemory(redis_client=FakeRedis())


def _event(
    sid: str,
    *,
    topics=(),
    msg: int = 1,
    ts: float = 0.0,
    prefs=None,
):
    """会话事件（MemorySummary.to_dict() 形状 + 可选 preferences）。"""
    data = {
        "session_id": sid,
        "user_id": "u1",
        "summary": f"摘要-{sid}",
        "topics": list(topics),
        "key_facts": [],
        "message_count": msg,
        "created_at": ts,
        "access_count": 0,
        "last_accessed": ts,
        "source_message_ids": [],
    }
    if prefs is not None:
        data["preferences"] = dict(prefs)
    return data


def _install_singleton(monkeypatch) -> LongTermMemory:
    mem = _memory()
    monkeypatch.setattr(ltm_module, "_long_term_memory_instance", mem)
    return mem


async def _seed_summary(mem, sid: str, *, topics, age_days: float, msg: int = 1):
    now = time.time()
    created = now - age_days * DAY
    payload = _event(sid, topics=topics, msg=msg, ts=created)
    await mem._redis.set(f"ltm:summary:{sid}", json.dumps(payload))
    await mem._redis.zadd("ltm:user:u1:sessions", {sid: created})


# ---------------------------------------------------------------------------
# 1. flag-off：序列化与 prompt 拼装与基线字节等价
# ---------------------------------------------------------------------------


class TestFlagOffByteEquivalence:
    def test_default_flag_is_off(self):
        assert app_settings.MEMORY_PROFILE_CARD_ENABLED is False

    def test_to_dict_matches_baseline_seven_keys(self, monkeypatch):
        monkeypatch.setattr(app_settings, "MEMORY_PROFILE_CARD_ENABLED", False)
        profile = UserProfile(
            user_id="u1",
            total_sessions=3,
            total_messages=17,
            frequent_topics=["租赁"],
            preferences={"language": "zh"},
        )
        data = profile.to_dict()
        assert set(data) == BASELINE_KEYS, "flag-off 不得出现 profile_card 键"
        assert data == {
            "user_id": "u1",
            "total_sessions": 3,
            "total_messages": 17,
            "frequent_topics": ["租赁"],
            "preferences": {"language": "zh"},
            "last_active": profile.last_active,
            "created_at": profile.created_at,
        }
        # 序列化稳定（字节级往返）
        assert json.loads(json.dumps(data)) == data

    def test_stored_card_is_not_serialized_when_flag_off(self, monkeypatch):
        """即使对象/存储里已有 card，flag-off 写盘也不带（只增键受 flag 闸）。"""
        monkeypatch.setattr(app_settings, "MEMORY_PROFILE_CARD_ENABLED", False)
        profile = UserProfile(
            user_id="u1",
            profile_card={"schema_version": PROFILE_CARD_SCHEMA_VERSION},
        )
        assert "profile_card" not in profile.to_dict()

    @pytest.mark.asyncio
    async def test_context_prompt_has_no_card_section(self, monkeypatch):
        monkeypatch.setattr(app_settings, "MEMORY_PROFILE_CARD_ENABLED", False)
        monkeypatch.setattr(app_settings, "MEMORY_CARDS_ENABLED", False)
        mem = _memory()
        await _seed_summary(mem, "s1", topics=["租赁"], age_days=1)
        # 模拟历史数据：profile 里已存 card（旧开关期写入）
        await mem._redis.set(
            "ltm:profile:u1",
            json.dumps(
                {
                    "user_id": "u1",
                    "total_sessions": 1,
                    "total_messages": 1,
                    "frequent_topics": ["租赁"],
                    "preferences": {},
                    "last_active": time.time(),
                    "created_at": time.time(),
                    "profile_card": {"schema_version": 1, "user_constraints": {}},
                }
            ),
        )

        context = await mem.get_context_for_query("租赁", user_id="u1")

        assert "profile_card" not in context
        assert "用户画像卡" not in context
        assert "用户历史" in context, "笔记轨/旧画像行必须保留（双轨）"


# ---------------------------------------------------------------------------
# 2. flag-on：roundtrip + 旧数据读侧兼容
# ---------------------------------------------------------------------------


class TestCardRoundtrip:
    def test_roundtrip_with_card(self, monkeypatch):
        monkeypatch.setattr(app_settings, "MEMORY_PROFILE_CARD_ENABLED", True)
        card = fold_profile_card(None, _event("s1", topics=["租赁"], msg=4, ts=1.0))
        profile = UserProfile(user_id="u1", total_sessions=1, profile_card=card)

        data = profile.to_dict()
        assert data["profile_card"] == card
        assert set(data) - {"profile_card"} == BASELINE_KEYS, "旧键一个不动"

        restored = UserProfile.from_dict(json.loads(json.dumps(data)))
        assert restored.profile_card == profile.profile_card
        assert restored.to_dict() == data, "roundtrip 稳定"

    def test_old_data_without_card_reads_fallback(self, monkeypatch):
        monkeypatch.setattr(app_settings, "MEMORY_PROFILE_CARD_ENABLED", True)
        legacy = {
            "user_id": "u2",
            "total_sessions": 5,
            "total_messages": 50,
            "frequent_topics": ["criminal"],
            "preferences": {"language": "zh"},
        }
        profile = UserProfile.from_dict(dict(legacy))
        assert profile.profile_card is None, "无 card 旧数据读侧回退旧字段"
        assert profile.total_sessions == 5
        assert profile.preferences == {"language": "zh"}
        # 对象有字段但为 None ⇒ 即使 flag 开也不写空卡
        assert "profile_card" not in profile.to_dict()


# ---------------------------------------------------------------------------
# 3. fold / derive：纯函数派生、幂等、冲突只标记不覆写
# ---------------------------------------------------------------------------


class TestFoldAndDerive:
    def test_single_stable_preference_tracked(self):
        card = fold_profile_card(
            None, _event("s1", ts=1.0, prefs={"语言": "中文"})
        )
        card = fold_profile_card(card, _event("s2", ts=2.0, prefs={"语言": "中文"}))
        assert card["user_constraints"] == {"语言": "中文"}
        assert card["conflicts"] == []

    def test_conflict_marked_not_overwritten(self):
        """同键不同值：原值保留在 user_constraints（不覆写），冲突只标记。"""
        card = fold_profile_card(
            None, _event("s1", ts=1.0, prefs={"答复风格": "简洁"})
        )
        card = fold_profile_card(
            card, _event("s2", ts=2.0, prefs={"答复风格": "详尽"})
        )
        assert card["user_constraints"]["答复风格"] == "简洁", "原值不得被覆写"
        rows = [row for row in card["conflicts"] if row["key"] == "答复风格"]
        assert len(rows) == 1
        assert set(rows[0]["values"]) == {"简洁", "详尽"}

        # 第三个值只并入既有冲突行，仍不覆写
        card = fold_profile_card(
            card, _event("s3", ts=3.0, prefs={"答复风格": "中英双语"})
        )
        assert card["user_constraints"]["答复风格"] == "简洁"
        rows = [row for row in card["conflicts"] if row["key"] == "答复风格"]
        assert set(rows[0]["values"]) == {"简洁", "详尽", "中英双语"}
        assert len(card["conflicts"]) == 1, "同键只保留一条冲突行"

    def test_fold_does_not_mutate_input(self):
        card1 = fold_profile_card(None, _event("s1", topics=["a"], ts=1.0))
        snapshot = json.loads(json.dumps(card1))
        card2 = fold_profile_card(card1, _event("s2", topics=["b"], ts=2.0))
        assert card1 == snapshot, "输入对象不得被修改"
        assert card2 is not card1

    def test_domain_counts_dedup_within_event_and_avg(self):
        card = fold_profile_card(
            None, _event("s1", topics=["租赁", "租赁", "合同"], msg=3, ts=1.0)
        )
        card = fold_profile_card(card, _event("s2", topics=["租赁"], msg=0, ts=2.0))
        assert card["legal_domain_prefs"] == [
            {"topic": "租赁", "count": 2},
            {"topic": "合同", "count": 1},
        ], "按 (-count, topic) 排序；事件内主题去重"
        usage = card["usage_habits"]
        assert usage["sessions"] == 2
        assert usage["messages"] == 3
        assert usage["avg_messages_per_session"] == 1.5
        assert usage["first_active"] == 1.0
        assert usage["last_active"] == 2.0

    def test_derive_order_independent_for_same_log(self):
        events = [
            _event("s1", topics=["租赁"], ts=1.0, prefs={"语言": "中文"}),
            _event("s2", topics=["合同"], ts=2.0, prefs={"语言": "中文"}),
            _event("s3", topics=["租赁"], ts=3.0, prefs={"格式": "表格"}),
        ]
        forward = derive_profile(events, user_id="u1")
        reverse = derive_profile(list(reversed(events)), user_id="u1")
        assert forward.profile_card == reverse.profile_card, "重放与输入次序无关"

    @pytest.mark.asyncio
    async def test_replay_equals_incremental_and_is_deterministic(self, monkeypatch):
        """派生幂等：增量维护 ≡ 事件日志重放；重放两次结果一致。"""
        monkeypatch.setattr(app_settings, "MEMORY_PROFILE_CARD_ENABLED", True)
        mem = _memory()
        for i, topics in (
            (1, ["租赁"]),
            (2, ["租赁", "合同"]),
            (3, ["民法"]),
        ):
            await mem.store_summary(
                f"s{i}",
                f"摘要{i}",
                [{"id": f"m{i}-1"}, {"id": f"m{i}-2"}],
                user_id="u1",
                topics=topics,
            )

        profile = await mem.get_user_profile("u1")
        assert profile is not None and profile.profile_card is not None
        incremental = profile.profile_card

        events = [s.to_dict() for s in await mem._load_all_summaries("u1")]
        replay1 = derive_profile(events, user_id="u1").profile_card
        replay2 = derive_profile(events, user_id="u1").profile_card

        assert replay1 == replay2, "纯函数必须确定性"
        assert incremental == replay1, "replay 结果 ≡ 增量维护结果"
        assert incremental["usage_habits"]["sessions"] == 3

    def test_derive_empty_log_has_no_card(self):
        assert derive_profile([], user_id="u1").profile_card is None
        assert derive_profile(["junk"], user_id="u1").profile_card is None, (
            "非 dict 事件忽略；不伪造空卡"
        )

    @pytest.mark.asyncio
    async def test_legacy_frequent_topics_cap_preserved_in_replay(self):
        """重派生沿用增量规则：首见序、容量 50（旧字段语义不漂移）。"""
        events = [
            _event(f"s{i:02d}", topics=[f"t{i:02d}"], ts=float(i)) for i in range(60)
        ]
        derived = derive_profile(events, user_id="u1")
        assert len(derived.frequent_topics) == 50
        assert derived.frequent_topics[0] == "t10", "保留最近 50 个首见主题"
        assert derived.total_sessions == 60
        assert derived.last_active == 59.0
        assert derived.created_at == 0.0


# ---------------------------------------------------------------------------
# 4. 触发器：memory_consolidation 才重派生；dry_run/空日志零写入
# ---------------------------------------------------------------------------


class TestConsolidationTrigger:
    @pytest.mark.asyncio
    async def test_flag_off_report_shape_unchanged(self, monkeypatch):
        monkeypatch.setattr(app_settings, "MEMORY_CONSOLIDATION_ENABLED", True)
        monkeypatch.setattr(app_settings, "MEMORY_PROFILE_CARD_ENABLED", False)
        mem = _install_singleton(monkeypatch)
        await _seed_summary(mem, "s1", topics=["租赁"], age_days=1)
        await _seed_summary(mem, "s2", topics=["合同"], age_days=1)

        result = await handle_memory_consolidation({"user_id": "u1"})

        assert result["status"] == "consolidated"
        assert "profile_card" not in result, "flag-off 报告形状与基线一致"
        assert "ltm:profile:u1" not in mem._redis.store, "flag-off 零 profile 写入"

    @pytest.mark.asyncio
    async def test_flag_on_derives_card_into_report_and_profile(self, monkeypatch):
        monkeypatch.setattr(app_settings, "MEMORY_CONSOLIDATION_ENABLED", True)
        monkeypatch.setattr(app_settings, "MEMORY_PROFILE_CARD_ENABLED", True)
        mem = _install_singleton(monkeypatch)
        await _seed_summary(mem, "s1", topics=["租赁"], age_days=1, msg=3)
        await _seed_summary(mem, "s2", topics=["合同"], age_days=1, msg=4)

        result = await handle_memory_consolidation({"user_id": "u1"})

        card = result.get("profile_card")
        assert card is not None, "flag 开且有事件 ⇒ 报告必须带派生卡片"
        assert card["schema_version"] == PROFILE_CARD_SCHEMA_VERSION
        assert card["usage_habits"]["sessions"] == 2
        assert card["usage_habits"]["messages"] == 7
        stored = json.loads(await mem._redis.get("ltm:profile:u1"))
        assert stored["profile_card"] == card, "卡片必须落盘"
        # 旧字段仍在（只增不改）
        assert set(stored) - {"profile_card"} == BASELINE_KEYS

    @pytest.mark.asyncio
    async def test_dry_run_never_writes_card(self, monkeypatch):
        monkeypatch.setattr(app_settings, "MEMORY_CONSOLIDATION_ENABLED", True)
        monkeypatch.setattr(app_settings, "MEMORY_PROFILE_CARD_ENABLED", True)
        mem = _install_singleton(monkeypatch)
        await _seed_summary(mem, "s1", topics=["租赁"], age_days=1)

        result = await handle_memory_consolidation({"user_id": "u1", "dry_run": True})

        assert result["dry_run"] is True
        assert "profile_card" not in result, "dry_run 零写入约定不被卡片破坏"
        assert "ltm:profile:u1" not in mem._redis.store
        assert "ltm:consolidation:last:u1" not in mem._redis.store

    @pytest.mark.asyncio
    async def test_empty_event_log_yields_no_card_key(self, monkeypatch):
        monkeypatch.setattr(app_settings, "MEMORY_CONSOLIDATION_ENABLED", True)
        monkeypatch.setattr(app_settings, "MEMORY_PROFILE_CARD_ENABLED", True)
        _install_singleton(monkeypatch)

        result = await handle_memory_consolidation({"user_id": "nobody"})

        assert result["status"] == "consolidated"
        assert result["before"] == 0
        assert "profile_card" not in result, "空日志不伪造卡片"

    @pytest.mark.asyncio
    async def test_eviction_archive_red_line_survives_card_flag(self, monkeypatch):
        """Phase 24 归档红线在 Phase 29 开关开时同样成立。"""
        monkeypatch.setattr(app_settings, "MEMORY_CONSOLIDATION_ENABLED", True)
        monkeypatch.setattr(app_settings, "MEMORY_PROFILE_CARD_ENABLED", True)
        mem = _install_singleton(monkeypatch)
        await _seed_summary(mem, "old", topics=["租赁"], age_days=400)
        await _seed_summary(mem, "new", topics=["合同"], age_days=5)

        result = await handle_memory_consolidation({"user_id": "u1"})

        assert result["evicted"] == 1
        assert result["archived_session_ids"] == ["old"]
        archived_raw = await mem._redis.get("ltm:archived:summary:old")
        assert archived_raw is not None, "淘汰红线：先归档正文再删原键"
        assert await mem._redis.get("ltm:summary:old") is None
        card = result.get("profile_card")
        assert card is not None
        assert card["usage_habits"]["sessions"] == 1, "卡片只反映保留集（当前日志）"


# ---------------------------------------------------------------------------
# 5. prompt 拼装：flag-off 等价、flag-on 出卡片段
# ---------------------------------------------------------------------------


class TestPromptAssembly:
    @pytest.mark.asyncio
    async def test_flag_on_context_includes_card_section(self, monkeypatch):
        monkeypatch.setattr(app_settings, "MEMORY_CARDS_ENABLED", False)
        monkeypatch.setattr(app_settings, "MEMORY_PROFILE_CARD_ENABLED", True)
        mem = _memory()
        await mem.store_summary(
            "s1", "摘要1", [{"id": "m1"}], user_id="u1", topics=["租赁"]
        )

        context = await mem.get_context_for_query("租赁", user_id="u1")

        assert "用户画像卡（profile_card）" in context
        assert '"schema_version"' in context
        assert "用户历史" in context and "相关历史对话摘要" in context, "笔记轨保留"
