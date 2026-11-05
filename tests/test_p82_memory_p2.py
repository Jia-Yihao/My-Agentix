# -*- coding: utf-8 -*-
"""F2a · Stage F2：Memory P2 守卫（M-P2-2 PreCompact 重注 / M-P2-4 画像作用域）。

两枚 flag 默认关、flag-off 与基线逐字节/逐位等价；授权边界 fail-closed
（无授权证据 ⇒ 不沿用稳定层，读取异常同样扣下）；删除面不静默吞错
（``errors`` 非空 ⇒ ops 端点显式 500）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentix.agent.context.compaction import (
    PRECOMPACT_CONSTRAINT_MARK,
    PRECOMPACT_RULE_MARK,
    build_reinject_block,
    precompact_reinject_enabled,
)
from agentix.agent.context.engine import ContextEngine
from agentix.agent.context.simple import SimpleContextBuilder
from agentix.application.services import long_term_memory as ltm_mod
from agentix.application.services.long_term_memory import (
    LongTermMemory,
    MemorySummary,
)
from agentix.application.services.profile_scope import (
    PROFILE_FIELD_TIERS,
    PROFILE_META_FIELDS,
    TIER_META,
    TIER_SESSION,
    TIER_STABLE,
    project_profile_card,
    project_profile_topics,
    tier_of,
)
from agentix.config import settings
from agentix.domain.agent.types import Observation, TaskSpec
from agentix.infrastructure.core.security import verify_api_key
from agentix.interfaces.http import ops_router

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ===========================================================================
# 1. Flag 五处同步：settings / selfcheck / .env.example / p41 / 配置管理.md
# ===========================================================================


class TestFlagSync:
    NEW_KEYS = ("CONTEXT_PRECOMPACT_REINJECT_ENABLED", "MEMORY_PROFILE_SCOPE_ENABLED")

    def test_settings_defaults_off(self):
        assert settings.CONTEXT_PRECOMPACT_REINJECT_ENABLED is False
        assert settings.MEMORY_PROFILE_SCOPE_ENABLED is False

    def test_selfcheck_ignores_missing(self):
        from agentix.config.selfcheck import DEFAULT_IGNORED_MISSING

        for key in self.NEW_KEYS:
            assert key in DEFAULT_IGNORED_MISSING

    def test_env_example_documents_keys(self):
        text = _read(".env.example")
        for key in self.NEW_KEYS:
            assert f"{key}=false" in text, key

    def test_p41_lists_keys(self):
        text = _read("tests/test_p41_doc_consistency.py")
        for key in self.NEW_KEYS:
            assert f'"{key}"' in text, key

    def test_configuration_md_documents_keys(self):
        text = _read("docs/architecture/配置管理.md")
        for key in self.NEW_KEYS:
            assert f"`{key}`" in text, key


# ===========================================================================
# 2. M-P2-2 纯函数：build_reinject_block（约束优先 / 去重 / 有界截断）
# ===========================================================================


class TestBuildReinjectBlock:
    def test_constraints_first_then_rules_with_counts(self):
        block, counts = build_reinject_block(
            ["RULE A", "RULE B"],
            ["x constraint=k1 y", "constraint=k1 again", "constraint=k2"],
        )
        assert block[0].startswith(PRECOMPACT_CONSTRAINT_MARK)
        assert block[1].startswith(PRECOMPACT_CONSTRAINT_MARK)
        assert block[2].startswith(PRECOMPACT_RULE_MARK)
        assert block[3].startswith(PRECOMPACT_RULE_MARK)
        assert "constraint=k1" in block[0] and "constraint=k2" in block[1]
        assert counts == {"rule": 2, "constraint": 2}

    def test_constraint_tokens_deduped_keep_first_occurrence_order(self):
        block, counts = build_reinject_block(
            [], ["constraint=k9 tail constraint=k9", "constraint=k1"]
        )
        rows = [r for r in block if r.startswith(PRECOMPACT_CONSTRAINT_MARK)]
        assert len(rows) == 2
        assert "k9" in rows[0] and "k1" in rows[1]
        assert counts["constraint"] == 2

    def test_max_chars_truncates_and_never_exceeds(self):
        block, counts = build_reinject_block(
            ["RULE A"], ["constraint=k1"], max_chars=40
        )
        assert len(block) == 1, "首行 38 字符 ⇒ 规则行超限短路"
        assert counts == {"rule": 0, "constraint": 1}
        assert sum(len(r) + 1 for r in block) <= 40

    def test_rule_budget_stops_at_first_row_that_does_not_fit(self):
        block, counts = build_reinject_block(
            ["RULE A", "RULE B"], [], max_chars=49
        )
        assert len(block) == 1 and block[0].endswith("RULE A")
        assert counts == {"rule": 1, "constraint": 0}

    def test_empty_sources_yield_empty_block(self):
        block, counts = build_reinject_block([], [])
        assert block == [] and counts == {"rule": 0, "constraint": 0}

    def test_blank_rule_lines_skipped(self):
        block, counts = build_reinject_block(["   ", "RULE B"], ["nothing here"])
        assert block == [f"{PRECOMPACT_RULE_MARK} RULE B"]
        assert counts == {"rule": 1, "constraint": 0}


# ===========================================================================
# 3. M-P2-2 接线：ContextEngine / SimpleContextBuilder
# ===========================================================================

_TASK = TaskSpec(goal="ship the feature")
_OBS = [Observation(step_id="s1", content="detail constraint=cap-30k must hold", ok=True)]
_RULES = ["R1"]


def _reinject_on(monkeypatch, *, autocompact: bool = True) -> None:
    monkeypatch.setattr(settings, "CONTEXT_PRECOMPACT_REINJECT_ENABLED", True)
    monkeypatch.setattr(settings, "CONTEXT_AUTOCOMPACT", autocompact)


class TestEngineReinject:
    @pytest.mark.asyncio
    async def test_flag_off_over_budget_has_no_block(self, monkeypatch):
        monkeypatch.setattr(settings, "CONTEXT_PRECOMPACT_REINJECT_ENABLED", False)
        eng = ContextEngine(system_rules=_RULES)
        snap = await eng.build(_TASK, trajectory=_OBS, budget_tokens=1)
        assert "[precompact" not in snap.prompt
        assert "precompact_reinject" not in snap.meta
        assert snap.prompt.count("RULE R1") == 1, "flag-off 规则只在前缀出现一次"
        assert snap.truncated is True

    @pytest.mark.asyncio
    async def test_flag_on_tail_block_survives_drop_loop_and_meta_counts(
        self, monkeypatch
    ):
        _reinject_on(monkeypatch)
        eng = ContextEngine(system_rules=_RULES)
        snap = await eng.build(_TASK, trajectory=_OBS, budget_tokens=1)
        lines = snap.prompt.splitlines()
        assert lines[-1] == f"{PRECOMPACT_RULE_MARK} RULE R1"
        assert f"{PRECOMPACT_CONSTRAINT_MARK} constraint=cap-30k" in snap.prompt
        assert snap.meta["precompact_reinject"] == {"rule": 1, "constraint": 1}
        assert "detail constraint" not in snap.prompt, "丢弃循环清空轨迹"
        assert snap.truncated is True
        assert snap.prompt.count("RULE R1") == 2, "前缀 + 重注块各一次"

    @pytest.mark.asyncio
    async def test_flag_on_requires_autocompact_channel(self, monkeypatch):
        _reinject_on(monkeypatch, autocompact=False)
        eng = ContextEngine(system_rules=_RULES)
        snap = await eng.build(_TASK, trajectory=_OBS, budget_tokens=1)
        assert "[precompact" not in snap.prompt
        assert "precompact_reinject" not in snap.meta

    @pytest.mark.asyncio
    async def test_flag_on_under_budget_no_block(self, monkeypatch):
        _reinject_on(monkeypatch)
        eng = ContextEngine(system_rules=_RULES)
        snap = await eng.build(_TASK, trajectory=_OBS, budget_tokens=0)
        assert "[precompact" not in snap.prompt
        assert "precompact_reinject" not in snap.meta
        assert "detail constraint" in snap.prompt, "未超预算 ⇒ 轨迹原样保留"

    @pytest.mark.asyncio
    async def test_flag_on_but_under_budget_equals_flag_off_bytes(self, monkeypatch):
        """同输入：flag-on（未超预算）与 flag-off 的 prompt/meta 逐字节一致。"""
        off = ContextEngine(system_rules=_RULES)
        _reinject_on(monkeypatch)
        on = ContextEngine(system_rules=_RULES)
        s_off = await off.build(_TASK, trajectory=_OBS, budget_tokens=0)
        s_on = await on.build(_TASK, trajectory=_OBS, budget_tokens=0)
        assert s_off.prompt == s_on.prompt
        assert s_off.meta == s_on.meta

    @pytest.mark.asyncio
    async def test_flag_on_without_rules_still_reinjects_constraints(self, monkeypatch):
        _reinject_on(monkeypatch)
        eng = ContextEngine()
        snap = await eng.build(_TASK, trajectory=_OBS, budget_tokens=1)
        assert f"{PRECOMPACT_CONSTRAINT_MARK} constraint=cap-30k" in snap.prompt
        assert snap.meta["precompact_reinject"] == {"rule": 0, "constraint": 1}

    def test_reader_fail_safe_when_settings_unreadable(self, monkeypatch):
        import importlib

        settings_mod = importlib.import_module("agentix.config.settings")
        monkeypatch.setattr(settings_mod, "settings", None)
        assert precompact_reinject_enabled() is False


class TestSimpleBuilderReinject:
    @pytest.mark.asyncio
    async def test_flag_on_tail_block_and_meta(self, monkeypatch):
        _reinject_on(monkeypatch)
        builder = SimpleContextBuilder(rules=list(_RULES))
        snap = await builder.build(_TASK, trajectory=_OBS, budget_tokens=1)
        lines = snap.prompt.splitlines()
        assert lines[-1] == f"{PRECOMPACT_RULE_MARK} R1"
        assert f"{PRECOMPACT_CONSTRAINT_MARK} constraint=cap-30k" in snap.prompt
        assert snap.meta == {"precompact_reinject": {"rule": 1, "constraint": 1}}
        assert "detail constraint" not in snap.prompt

    @pytest.mark.asyncio
    async def test_flag_off_rules_appear_once_and_meta_empty(self, monkeypatch):
        monkeypatch.setattr(settings, "CONTEXT_PRECOMPACT_REINJECT_ENABLED", False)
        builder = SimpleContextBuilder(rules=list(_RULES))
        snap = await builder.build(_TASK, trajectory=_OBS, budget_tokens=1)
        assert "[precompact" not in snap.prompt
        assert snap.prompt.count("R1") == 1
        assert snap.meta == {}


# ===========================================================================
# 4. M-P2-4 纯函数：字段分级与投影
# ===========================================================================


class TestProfileTierProjection:
    def test_tier_table_values_within_known_tiers(self):
        assert set(PROFILE_FIELD_TIERS.values()) <= {
            TIER_STABLE,
            TIER_SESSION,
            TIER_META,
        }

    def test_known_field_tiers(self):
        assert tier_of("usage_habits") == TIER_SESSION
        assert tier_of("schema_version") == TIER_META
        assert tier_of("user_constraints") == TIER_STABLE
        assert tier_of("legal_domain_prefs") == TIER_STABLE
        assert tier_of("conflicts") == TIER_STABLE

    def test_unknown_field_fails_closed_as_stable(self):
        assert tier_of("brand_new_field") == TIER_STABLE

    def test_meta_fields_contains_schema_version(self):
        assert "schema_version" in PROFILE_META_FIELDS

    def test_project_allow_stable_returns_equal_copy(self):
        card = {
            "schema_version": 1,
            "usage_habits": {"q": 5},
            "user_constraints": {"d": "ZM9"},
        }
        out = project_profile_card(card, allow_stable=True)
        assert out == card and out is not card

    def test_project_denied_keeps_only_session_and_meta(self):
        card = {
            "schema_version": 1,
            "usage_habits": {"q": 5},
            "user_constraints": {"d": "ZM9"},
            "legal_domain_prefs": ["合同"],
            "conflicts": [],
            "future_field": "x",
        }
        out = project_profile_card(card, allow_stable=False)
        assert set(out) == {"schema_version", "usage_habits"}

    def test_project_non_dict_is_empty_card(self):
        assert project_profile_card(None, allow_stable=True) == {}
        assert project_profile_card("x", allow_stable=False) == {}

    def test_project_topics_gating(self):
        assert project_profile_topics(["a", "b"], allow_stable=True) == ["a", "b"]
        assert project_profile_topics(["a"], allow_stable=False) == []


# ===========================================================================
# 5. M-P2-4：授权读写 + get_context_for_query 投影门控
# ===========================================================================


class _FakeRedis:
    """长时记忆所需最小面 + 按键故障注入（授权 fail-closed / 分段清除用）。"""

    def __init__(self):
        self.store: dict = {}
        self.zsets: dict = {}
        self.fail_get_keys: set = set()
        self.fail_delete_keys: set = set()

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def get(self, key):
        if key in self.fail_get_keys:
            raise RuntimeError("redis get unavailable")
        return self.store.get(key)

    async def delete(self, key):
        if key in self.fail_delete_keys:
            raise RuntimeError("redis delete unavailable")
        removed = 0
        if self.store.pop(key, None) is not None:
            removed += 1
        if self.zsets.pop(key, None) is not None:
            removed += 1
        return removed

    async def hset(self, key, mapping=None, **kw):
        self.store.setdefault(key, {}).update(mapping or {})

    async def hgetall(self, key):
        return dict(self.store.get(key, {}))

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    async def expire(self, key, ttl):
        return True

    async def zrevrange(self, key, start, end):
        items = sorted(self.zsets.get(key, {}).items(), key=lambda kv: -kv[1])
        return [k for k, _ in items[start : end + 1 if end >= 0 else None]]

    async def zrange(self, key, start, end):
        items = sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1])
        seq = [k for k, _ in items]
        stop = len(seq) - 1 if end == -1 else end
        return seq[start : stop + 1]


_SCOPE_KEY = "ltm:profile_scope:u1"

_PROFILE = {
    "user_id": "u1",
    "total_sessions": 7,
    "total_messages": 42,
    "frequent_topics": ["TOPIC-LY4", "合规"],
    "preferences": {},
    "last_active": 1.0,
    "created_at": 1.0,
    "profile_card": {
        "schema_version": 1,
        "usage_habits": {"daily_queries": 5},
        "user_constraints": {"hard_deadline": "ZM9-deadline"},
        "legal_domain_prefs": ["合同"],
        "conflicts": [],
    },
}


def _seed_scope_data(redis: _FakeRedis) -> None:
    mem = MemorySummary(
        session_id="sess1",
        user_id="u1",
        summary="客户要求三日内交付合同",
        topics=["合同"],
    )
    redis.store["ltm:summary:sess1"] = json.dumps(mem.to_dict())
    redis.zsets["ltm:user:u1:sessions"] = {"sess1": mem.created_at}
    redis.store["ltm:profile:u1"] = json.dumps(_PROFILE, ensure_ascii=False)
    from agentix.application.services.semantic_memory import MemoryCard, _dumps

    redis.store["ltm:cards:u1"] = {
        "c1": _dumps(
            MemoryCard(
                card_id="c1",
                card_type="preference",
                content="always-reply-english-KW8",
            )
        )
    }


def _scoped_ltm() -> tuple:
    redis = _FakeRedis()
    _seed_scope_data(redis)
    return LongTermMemory(redis_client=redis, summary_ttl=60), redis


class TestConsentReadWrite:
    @pytest.mark.asyncio
    async def test_consent_roundtrip(self):
        ltm, redis = _scoped_ltm()
        assert await ltm.profile_scope_consent("u1") is False
        await ltm.set_profile_scope_consent("u1", granted=True)
        assert await ltm.profile_scope_consent("u1") is True
        assert redis.store[_SCOPE_KEY] == "granted"
        await ltm.set_profile_scope_consent("u1", granted=False)
        assert await ltm.profile_scope_consent("u1") is False
        assert _SCOPE_KEY not in redis.store

    @pytest.mark.asyncio
    async def test_consent_read_failure_fails_closed(self):
        ltm, redis = _scoped_ltm()
        redis.fail_get_keys.add(_SCOPE_KEY)
        assert await ltm.profile_scope_consent("u1") is False


class TestGetContextScopeGating:
    @pytest.mark.asyncio
    async def test_flag_off_injects_full_without_consulting_consent(
        self, monkeypatch
    ):
        monkeypatch.setattr(settings, "MEMORY_PROFILE_CARD_ENABLED", True)
        monkeypatch.setattr(settings, "MEMORY_PROFILE_SCOPE_ENABLED", False)
        ltm, redis = _scoped_ltm()
        # 授权键故障注入：flag-off 若去读授权 ⇒ fail-closed 会扣下稳定层
        redis.fail_get_keys.add(_SCOPE_KEY)
        ctx = await ltm.get_context_for_query("合同", user_id="u1")
        assert "ZM9-deadline" in ctx
        assert "TOPIC-LY4" in ctx
        assert "always-reply-english-KW8" in ctx
        assert "daily_queries" in ctx
        assert "相关历史对话摘要" in ctx

    @pytest.mark.asyncio
    async def test_flag_on_without_consent_withholds_stable_layer(self, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_PROFILE_CARD_ENABLED", True)
        monkeypatch.setattr(settings, "MEMORY_PROFILE_SCOPE_ENABLED", True)
        ltm, _ = _scoped_ltm()
        ctx = await ltm.get_context_for_query("合同", user_id="u1")
        assert "ZM9-deadline" not in ctx, "稳定层画像卡必须扣下"
        assert "TOPIC-LY4" not in ctx, "频繁领域 = 稳定层"
        assert "always-reply-english-KW8" not in ctx, "语义卡 = 稳定层"
        assert "daily_queries" in ctx, "会话层字段保留"
        assert "schema_version" in ctx, "meta 字段永远保留"
        assert "关注领域：未记录" in ctx
        assert "共 7 次会话" in ctx
        assert "相关历史对话摘要" in ctx, "摘要是会话语义，不受画像作用域影响"

    @pytest.mark.asyncio
    async def test_flag_on_with_consent_injects_stable_layer(self, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_PROFILE_CARD_ENABLED", True)
        monkeypatch.setattr(settings, "MEMORY_PROFILE_SCOPE_ENABLED", True)
        ltm, _ = _scoped_ltm()
        await ltm.set_profile_scope_consent("u1", granted=True)
        ctx = await ltm.get_context_for_query("合同", user_id="u1")
        assert "ZM9-deadline" in ctx
        assert "TOPIC-LY4" in ctx
        assert "always-reply-english-KW8" in ctx
        assert "daily_queries" in ctx

    @pytest.mark.asyncio
    async def test_flag_on_consent_read_error_fails_closed(self, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_PROFILE_CARD_ENABLED", True)
        monkeypatch.setattr(settings, "MEMORY_PROFILE_SCOPE_ENABLED", True)
        ltm, redis = _scoped_ltm()
        redis.fail_get_keys.add(_SCOPE_KEY)
        ctx = await ltm.get_context_for_query("合同", user_id="u1")
        assert "ZM9-deadline" not in ctx
        assert "always-reply-english-KW8" not in ctx

    @pytest.mark.asyncio
    async def test_flag_on_no_profile_only_cards_withheld(self, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_PROFILE_SCOPE_ENABLED", True)
        redis = _FakeRedis()
        from agentix.application.services.semantic_memory import MemoryCard, _dumps

        redis.store["ltm:cards:u1"] = {
            "c1": _dumps(
                MemoryCard(
                    card_id="c1", card_type="preference", content="only-card-KW8"
                )
            )
        }
        ltm = LongTermMemory(redis_client=redis, summary_ttl=60)
        ctx = await ltm.get_context_for_query("x", user_id="u1")
        assert ctx == "", "无摘要/画像且未授权 ⇒ 语义卡整段扣下"
        await ltm.set_profile_scope_consent("u1", granted=True)
        ctx2 = await ltm.get_context_for_query("x", user_id="u1")
        assert "only-card-KW8" in ctx2


# ===========================================================================
# 6. M-P2-4：一键清除全链路（分段计数 + 授权随画像撤销）
# ===========================================================================


class TestClearUserProfileChain:
    @pytest.mark.asyncio
    async def test_full_chain_cleared_with_per_segment_counts(self):
        ltm, redis = _scoped_ltm()
        await ltm.set_profile_scope_consent("u1", granted=True)
        report = await ltm.clear_user_profile_chain("u1")
        assert report["errors"] == []
        assert report["summaries"] == 1
        assert report["profile_cleared"] is True
        assert report["semantic_cards"] == 1
        assert report["scope_consent_revoked"] is True
        assert "ltm:summary:sess1" not in redis.store
        assert "ltm:user:u1:sessions" not in redis.zsets
        assert "ltm:profile:u1" not in redis.store
        assert "ltm:cards:u1" not in redis.store
        assert _SCOPE_KEY not in redis.store

    @pytest.mark.asyncio
    async def test_segment_failure_does_not_stop_later_segments(self):
        ltm, redis = _scoped_ltm()
        await ltm.set_profile_scope_consent("u1", granted=True)
        redis.fail_delete_keys.add("ltm:summary:sess1")
        report = await ltm.clear_user_profile_chain("u1")
        assert report["errors"], "失败段必须如实进 errors"
        assert any("summaries/profile" in e for e in report["errors"])
        assert report["semantic_cards"] == 1, "首段失败后后续段照常执行"
        assert "ltm:cards:u1" not in redis.store
        assert report["scope_consent_revoked"] is True
        assert _SCOPE_KEY not in redis.store
        assert "ltm:summary:sess1" in redis.store, "失败段不谎报已删"

    @pytest.mark.asyncio
    async def test_clear_without_consent_still_clears(self):
        ltm, redis = _scoped_ltm()
        report = await ltm.clear_user_profile_chain("u1")
        assert report["errors"] == []
        assert report["profile_cleared"] is True
        assert report["scope_consent_revoked"] is True


# ===========================================================================
# 7. ops 端点：profile-scope 授权面 + clear-user 删除面
# ===========================================================================


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(ops_router.router)
    app.dependency_overrides[verify_api_key] = lambda: None
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def ltm_env(monkeypatch):
    redis = _FakeRedis()
    inst = LongTermMemory(redis_client=redis, summary_ttl=60)
    monkeypatch.setattr(ltm_mod, "_long_term_memory_instance", inst)
    monkeypatch.setattr(settings, "MEMORY_PROFILE_SCOPE_ENABLED", False)
    return inst, redis


class TestOpsEndpoints:
    def test_profile_scope_get_reports_flag_and_consent(self, client, ltm_env):
        resp = client.get("/ops/memory/profile-scope", params={"user_id": "u1"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["user_id"] == "u1"
        assert body["profile_scope_enabled"] is False
        assert body["cross_session_consent"] is False

    def test_profile_scope_grant_then_revoke(self, client, ltm_env):
        resp = client.post(
            "/ops/memory/profile-scope",
            json={"user_id": "u1", "grant": True},
        )
        assert resp.status_code == 200
        assert resp.json()["cross_session_consent"] is True
        assert (
            client.get("/ops/memory/profile-scope", params={"user_id": "u1"}).json()[
                "cross_session_consent"
            ]
            is True
        )
        resp = client.post(
            "/ops/memory/profile-scope",
            json={"user_id": "u1", "grant": False},
        )
        assert resp.status_code == 200
        assert resp.json()["cross_session_consent"] is False
        assert (
            client.get("/ops/memory/profile-scope", params={"user_id": "u1"}).json()[
                "cross_session_consent"
            ]
            is False
        )

    def test_clear_user_returns_report(self, client, ltm_env):
        _, redis = ltm_env
        _seed_scope_data(redis)
        resp = client.post("/ops/memory/clear-user", json={"user_id": "u1"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["errors"] == []
        assert body["summaries"] == 1
        assert body["profile_cleared"] is True
        assert body["semantic_cards"] == 1
        assert body["scope_consent_revoked"] is True

    def test_clear_user_partial_failure_is_explicit_500(self, client, ltm_env):
        _, redis = ltm_env
        redis.store["ltm:profile:default_user"] = "{}"
        redis.fail_delete_keys.add("ltm:profile:default_user")
        resp = client.post("/ops/memory/clear-user", json={})
        assert resp.status_code == 500
        detail = resp.json()["detail"]
        assert "partial failure" in detail["message"]
        assert detail["report"]["errors"]

    def test_clear_user_empty_user_is_200_with_zero_counts(self, client, ltm_env):
        resp = client.post("/ops/memory/clear-user", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["errors"] == []
        assert body["summaries"] == 0
        assert body["semantic_cards"] == 0
