"""Unit tests for long-term memory module."""

from agentix.application.services.long_term_memory import (
    MemorySummary,
    UserProfile,
)


class TestMemorySummary:
    """Tests for MemorySummary dataclass."""

    def test_creation(self):
        mem = MemorySummary(
            session_id="sess1",
            user_id="user1",
            summary="User asked about contract law.",
            topics=["contract", "law"],
            key_facts=["Article 577 applies"],
            message_count=10,
        )
        assert mem.session_id == "sess1"
        assert mem.summary == "User asked about contract law."
        assert len(mem.topics) == 2
        assert mem.message_count == 10

    def test_to_dict(self):
        mem = MemorySummary(
            session_id="s1",
            user_id="u1",
            summary="test summary",
        )
        d = mem.to_dict()
        assert d["session_id"] == "s1"
        assert d["user_id"] == "u1"
        assert d["summary"] == "test summary"
        assert "created_at" in d

    def test_from_dict(self):
        data = {
            "session_id": "s2",
            "user_id": "u2",
            "summary": "another summary",
            "topics": ["t1"],
            "key_facts": ["f1", "f2"],
            "message_count": 5,
        }
        mem = MemorySummary.from_dict(data)
        assert mem.session_id == "s2"
        assert mem.topics == ["t1"]
        assert len(mem.key_facts) == 2

    def test_roundtrip(self):
        original = MemorySummary(
            session_id="rt",
            user_id="u",
            summary="roundtrip test",
            topics=["a", "b"],
            key_facts=["fact1"],
            message_count=3,
        )
        restored = MemorySummary.from_dict(original.to_dict())
        assert restored.session_id == original.session_id
        assert restored.summary == original.summary
        assert restored.topics == original.topics


class TestUserProfile:
    """Tests for UserProfile dataclass."""

    def test_creation(self):
        profile = UserProfile(
            user_id="user1",
            total_sessions=10,
            total_messages=100,
            frequent_topics=["contract", "tort"],
        )
        assert profile.user_id == "user1"
        assert profile.total_sessions == 10
        assert len(profile.frequent_topics) == 2

    def test_to_dict(self):
        profile = UserProfile(user_id="u1")
        d = profile.to_dict()
        assert d["user_id"] == "u1"
        assert d["total_sessions"] == 0
        assert d["frequent_topics"] == []

    def test_from_dict(self):
        data = {
            "user_id": "u2",
            "total_sessions": 5,
            "total_messages": 50,
            "frequent_topics": ["criminal"],
            "preferences": {"language": "zh"},
        }
        profile = UserProfile.from_dict(data)
        assert profile.total_sessions == 5
        assert profile.preferences == {"language": "zh"}

    def test_roundtrip(self):
        original = UserProfile(
            user_id="rt",
            total_sessions=3,
            total_messages=30,
            frequent_topics=["a", "b"],
            preferences={"theme": "dark"},
        )
        restored = UserProfile.from_dict(original.to_dict())
        assert restored.user_id == original.user_id
        assert restored.total_sessions == original.total_sessions
        assert restored.preferences == original.preferences


# ---------------------------------------------------------------------------
# 批次 7：recall 相关性打分加时效衰减（MEMORY_RECENCY_HALF_LIFE_DAYS）
#
# 旧行为：score = 关键词命中数，与摘要新旧无关。
# 新行为：score = 关键词命中数 × 0.5^(age_days / half_life)；
#         half_life <= 0 关闭衰减，与旧行为逐位一致。
# ---------------------------------------------------------------------------


class _FakeRedis:
    """只实现 recall 用到的三个命令的最小 Redis 假件。"""

    def __init__(self, summaries):
        self._summaries = summaries  # {session_id: MemorySummary}
        self._index = sorted(
            self._summaries.keys(),
            key=lambda sid: -self._summaries[sid].created_at,
        )

    async def zrevrange(self, key, start, stop):
        return self._index[start : stop + 1]

    async def get(self, key):
        sid = key.rsplit(":", 1)[-1]
        mem = self._summaries.get(sid)
        import json as _json

        return _json.dumps(mem.to_dict()) if mem else None

    async def set(self, key, value, ex=None):
        return True


def _make_mem(sid, summary, created_at):
    return MemorySummary(
        session_id=sid,
        user_id="u1",
        summary=summary,
        topics=[],
        created_at=created_at,
    )


class TestRecallRecencyDecay:
    NOW = 1_700_000_000.0

    def _build(self):
        # 老摘要命中 2 个词（60 天前）；新摘要只命中 1 个词（刚刚）
        old = _make_mem("s-old", "劳动合同违约责任与赔偿标准", self.NOW - 60 * 86400)
        new = _make_mem("s-new", "劳动合同的签订流程注意事项", self.NOW)
        return {"s-old": old, "s-new": new}

    def _recall(self, summaries, monkeypatch, half_life):
        import asyncio

        from agentix.application.services import long_term_memory as ltm
        from agentix.config import settings as app_settings

        # 注意：agentix.config.settings 导出的就是 Settings 实例（非模块），
        # 直接在实例上打补丁；long_term_memory 内部读的正是同一对象。
        monkeypatch.setattr(
            app_settings, "MEMORY_RECENCY_HALF_LIFE_DAYS", half_life
        )
        mem = ltm.LongTermMemory(
            redis_client=_FakeRedis(summaries), prefix="ltm"
        )
        return asyncio.run(
            mem.recall(user_id="u1", limit=5, query="劳动合同 违约")
        )

    def test_decay_lets_recent_memory_win_with_fewer_hits(self, monkeypatch):
        ordered = self._recall(self._build(), monkeypatch, half_life=14.0)
        # 老摘要命中 2 词但衰减 ~0.051；新摘要 1 词且因子 1.0 ⇒ 新摘要第一
        assert ordered[0].session_id == "s-new"
        assert ordered[1].session_id == "s-old"

    def test_decay_disabled_preserves_pure_keyword_ordering(self, monkeypatch):
        ordered = self._recall(self._build(), monkeypatch, half_life=0.0)
        # 关闭衰减 ⇒ 退化为旧行为：命中多者第一
        assert ordered[0].session_id == "s-old"

    def test_equal_hits_tie_breaks_toward_newer(self, monkeypatch):
        a = _make_mem("s-a", "劳动合同内容甲", self.NOW - 30 * 86400)
        b = _make_mem("s-b", "劳动合同内容乙", self.NOW)
        ordered = self._recall({"s-a": a, "s-b": b}, monkeypatch, half_life=14.0)
        # 同命中数时衰减使新者分高
        assert ordered[0].session_id == "s-b"
