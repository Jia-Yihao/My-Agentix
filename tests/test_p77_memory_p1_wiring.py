# -*- coding: utf-8 -*-
"""F1b · Stage F：Memory P1 接线守卫（M-P1-1 向量召回 / M-P1-2 后台评审 LLM 路由 / M-P1-5 真分词计数）。

对应 ``docs/design/记忆域深度优化.md`` §5 与统一执行链方案 §4 Stage F。
纪律：全部 flag 默认关、flag-off 与基线逐位等价；向量/LLM/tokenizer 任何
失败均 fail-open（断向量服务仍可答、评审不炸主链、估算不炸构建）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentix.application.services import memory_review
from agentix.application.services.memory_review import (
    TARGET_EPISODIC,
    TARGET_PROFILE,
    RouteDecision,
    REVIEW_ROUTE_PROMPT,
    review_items,
    route_memory_item,
    route_memory_item_llm,
    schedule_review_for_summary,
)
from agentix.agent.context import token_count
from agentix.agent.memory.service import InMemoryMemory
from agentix.config import settings
from agentix.domain.agent.types import MemoryItem, MemoryKind, MemoryScope
from agentix.domain.policies.memory_recall import (
    cosine_similarity,
    fuse_recall_scores,
    recency_weight,
)

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ===========================================================================
# 1. Flag 五处同步：settings / selfcheck / .env.example / p41 / 配置管理.md
# ===========================================================================


class TestFlagSync:
    NEW_KEYS = ("MEMORY_VECTOR_RECALL_ENABLED", "CONTEXT_REAL_TOKEN_COUNT_ENABLED")

    def test_settings_defaults_off(self):
        assert settings.MEMORY_VECTOR_RECALL_ENABLED is False
        assert settings.CONTEXT_REAL_TOKEN_COUNT_ENABLED is False

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

    def test_requirements_pins_tiktoken(self):
        text = _read("requirements.txt")
        assert "tiktoken==" in text


# ===========================================================================
# 2. M-P1-1 融合纯策略（domain/policies/memory_recall.py）
# ===========================================================================


class TestRecallPolicy:
    def test_cosine_known_values(self):
        assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
        assert cosine_similarity([1.0, 1.0], [1.0, 0.0]) == pytest.approx(0.7071, abs=1e-3)

    def test_cosine_degenerate_inputs(self):
        assert cosine_similarity([], [1.0]) == 0.0
        assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0
        assert cosine_similarity([1.0, 0.0], [1.0]) == 0.0  # 维度不匹配
        assert cosine_similarity([-1.0, 0.0], [1.0, 0.0]) == 0.0  # 负相关截 0

    def test_recency_weight_semantics(self):
        import time

        now = time.time()
        assert recency_weight(None, half_life_days=14.0, now=now) == 1.0
        assert recency_weight(now - 100, half_life_days=0.0, now=now) == 1.0
        assert recency_weight(now + 100, half_life_days=14.0, now=now) == 1.0  # 未来戳
        half = now - 14.0 * 86400.0
        assert recency_weight(half, half_life_days=14.0, now=now) == pytest.approx(0.5)
        quarter = now - 28.0 * 86400.0
        assert recency_weight(quarter, half_life_days=14.0, now=now) == pytest.approx(0.25)

    def test_fuse_dual_channel_beats_single(self):
        # 候选序保持首见序；双通道命中 > 仅向量高分；recency 参与乘法
        out = fuse_recall_scores(
            ["a", "b", "c"],
            keyword_hits={"a"},
            vector_scores={"a": 0.9, "b": 0.8, "c": 0.1},
            recency_weights={"a": 1.0, "b": 1.0, "c": 1.0},
        )
        ids = [k for k, _ in out]
        # a: 0.5*1 + 0.5*0.9 = 0.95；b: 0.5*0 + 0.5*0.8 = 0.4；c: 0.05
        assert ids[0] == "a"
        assert out[0][1] == pytest.approx(0.95)
        assert ids[1] == "b" and ids[2] == "c"

    def test_fuse_recency_scales_score(self):
        out = dict(
            fuse_recall_scores(
                ["old", "new"],
                keyword_hits=set(),
                vector_scores={"old": 0.8, "new": 0.8},
                recency_weights={"old": 0.5, "new": 1.0},
            )
        )
        assert out["new"] > out["old"]
        # (0.5·0 + 0.5·0.8) × recency
        assert out["old"] == pytest.approx(0.2)
        assert out["new"] == pytest.approx(0.4)

    def test_fuse_stable_tie_keeps_candidate_order(self):
        out = fuse_recall_scores(
            ["x", "y", "z"],
            keyword_hits=set(),
            vector_scores={"x": 0.5, "y": 0.5, "z": 0.5},
            recency_weights={},
        )
        assert [k for k, _ in out] == ["x", "y", "z"]


# ===========================================================================
# 3. M-P1-1 进程内 provider 向量召回（flag 门控 + fail-open）
# ===========================================================================


def _item(
    content: str,
    *,
    item_id: str,
    confidence: float = 1.0,
    stored_at: float | None = None,
    subject: str = "u1",
    tags: tuple[str, ...] = (),
) -> MemoryItem:
    meta = {} if stored_at is None else {"stored_at": stored_at}
    return MemoryItem(
        content=content,
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope.USER,
        item_id=item_id,
        subject=subject,
        confidence=confidence,
        tags=tags,
        meta=meta,
    )


def _seed(mem: InMemoryMemory, item: MemoryItem, vec: list[float]) -> None:
    mem._items.append(item)
    mem._vectors[item.item_id] = vec


class TestVectorRecall:
    @pytest.mark.asyncio
    async def test_flag_off_never_calls_embedding(self, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_VECTOR_RECALL_ENABLED", False)

        def _boom(texts, model=None, base_url=None):
            raise AssertionError("flag-off 不该触发 embedding")

        import agentix.retrieval.infra.rag.dense as dense

        monkeypatch.setattr(dense, "embed_texts_sync", _boom)

        mem = InMemoryMemory()
        await mem.remember(_item("用户偏好短答", item_id="a"))
        hits = await mem.recall(
            scope=MemoryScope.USER.value, subject="u1", query="短答"
        )
        assert [it.item_id for it in hits] == ["a"]
        assert mem._vectors == {}  # flag-off 恒不挂向量

    @pytest.mark.asyncio
    async def test_flag_on_semantic_match_without_substring(self, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_VECTOR_RECALL_ENABLED", True)
        import agentix.retrieval.infra.rag.dense as dense

        # 查询词不在正文里（子串路径召回不到），但向量同向
        monkeypatch.setattr(
            dense,
            "embed_texts_sync",
            lambda texts, model=None, base_url=None: [[1.0, 0.0]],
        )
        mem = InMemoryMemory()
        _seed(mem, _item("合同违约金上限约定", item_id="sem", tags=("t1",)), [1.0, 0.0])
        _seed(mem, _item("无关条目", item_id="far", tags=("t2",)), [0.0, 1.0])

        hits = await mem.recall(
            scope=MemoryScope.USER.value, subject="u1", query="teleport"
        )
        assert [it.item_id for it in hits] == ["sem"]

    @pytest.mark.asyncio
    async def test_flag_on_keyword_channel_ranked_first(self, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_VECTOR_RECALL_ENABLED", True)
        import agentix.retrieval.infra.rag.dense as dense

        monkeypatch.setattr(
            dense,
            "embed_texts_sync",
            lambda texts, model=None, base_url=None: [[1.0, 0.0]],
        )
        mem = InMemoryMemory()
        # 两件都与查询向量同向；"kw" 同时含查询子串 ⇒ 双通道应排第一。
        # 不同 tags = 不同冲突键，避免 resolve_conflicts 把两件裁成一件。
        _seed(mem, _item("vector only match", item_id="vec", tags=("t1",)), [1.0, 0.0])
        _seed(mem, _item("teleport kw match", item_id="kw", tags=("t2",)), [1.0, 0.0])

        hits = await mem.recall(
            scope=MemoryScope.USER.value, subject="u1", query="teleport"
        )
        assert [it.item_id for it in hits][:2] == ["kw", "vec"]

    @pytest.mark.asyncio
    async def test_embedding_failure_fails_open_to_keyword(self, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_VECTOR_RECALL_ENABLED", True)
        import agentix.retrieval.infra.rag.dense as dense

        def _down(texts, model=None, base_url=None):
            raise ConnectionError("embedding service down")

        monkeypatch.setattr(dense, "embed_texts_sync", _down)
        mem = InMemoryMemory()
        _seed(mem, _item("含关键词 的条目", item_id="k"), [1.0, 0.0])

        hits = await mem.recall(
            scope=MemoryScope.USER.value, subject="u1", query="关键词"
        )
        assert [it.item_id for it in hits] == ["k"]

    @pytest.mark.asyncio
    async def test_empty_query_embedding_fails_open(self, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_VECTOR_RECALL_ENABLED", True)
        import agentix.retrieval.infra.rag.dense as dense

        monkeypatch.setattr(
            dense, "embed_texts_sync", lambda texts, model=None, base_url=None: []
        )
        mem = InMemoryMemory()
        _seed(mem, _item("关键词条目", item_id="k"), [1.0, 0.0])
        hits = await mem.recall(
            scope=MemoryScope.USER.value, subject="u1", query="关键词"
        )
        assert [it.item_id for it in hits] == ["k"]

    @pytest.mark.asyncio
    async def test_remember_embed_failure_still_stores(self, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_VECTOR_RECALL_ENABLED", True)
        import agentix.retrieval.infra.rag.dense as dense

        def _down(texts, model=None, base_url=None):
            raise ConnectionError("down")

        monkeypatch.setattr(dense, "embed_texts_sync", _down)
        mem = InMemoryMemory()
        item_id = await mem.remember(_item("写入不受向量影响", item_id="w"))
        assert item_id == "w"
        assert mem.all_items()[0].item_id == "w"

    @pytest.mark.asyncio
    async def test_vector_path_still_redacts_pii(self, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_VECTOR_RECALL_ENABLED", True)
        import agentix.retrieval.infra.rag.dense as dense

        monkeypatch.setattr(
            dense,
            "embed_texts_sync",
            lambda texts, model=None, base_url=None: [[1.0, 0.0]],
        )
        mem = InMemoryMemory()
        # 直存原文（legacy 兜底场景）：读路径必须二次脱敏（M-P0-3 不因
        # 向量路径而失效）
        _seed(
            mem,
            _item("联系我 13812345678 处理合同", item_id="p"),
            [1.0, 0.0],
        )
        hits = await mem.recall(
            scope=MemoryScope.USER.value, subject="u1", query="teleport"
        )
        assert len(hits) == 1
        assert "13812345678" not in hits[0].content
        assert "[PHONE]" in hits[0].content

    @pytest.mark.asyncio
    async def test_recency_decay_orders_older_item_lower(self, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_VECTOR_RECALL_ENABLED", True)
        monkeypatch.setattr(settings, "MEMORY_RECENCY_HALF_LIFE_DAYS", 14.0)
        import time

        import agentix.retrieval.infra.rag.dense as dense

        monkeypatch.setattr(
            dense,
            "embed_texts_sync",
            lambda texts, model=None, base_url=None: [[1.0, 0.0]],
        )
        now = time.time()
        mem = InMemoryMemory()
        _seed(
            mem,
            _item(
                "同样的向量内容甲", item_id="old", stored_at=now - 60 * 86400, tags=("t1",)
            ),
            [1.0, 0.0],
        )
        _seed(
            mem,
            _item("同样的向量内容乙", item_id="new", stored_at=now - 1, tags=("t2",)),
            [1.0, 0.0],
        )
        hits = await mem.recall(
            scope=MemoryScope.USER.value, subject="u1", query="teleport"
        )
        assert [it.item_id for it in hits] == ["new", "old"]

    @pytest.mark.asyncio
    async def test_forget_prunes_vectors(self, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_VECTOR_RECALL_ENABLED", True)
        import agentix.retrieval.infra.rag.dense as dense

        monkeypatch.setattr(
            dense,
            "embed_texts_sync",
            lambda texts, model=None, base_url=None: [[1.0, 0.0]],
        )
        mem = InMemoryMemory()
        _seed(mem, _item("待删除条目", item_id="d"), [1.0, 0.0])
        removed = await mem.forget(
            scope=MemoryScope.USER.value, subject="u1", item_id="d"
        )
        assert removed == 1
        assert mem._vectors == {}

    @pytest.mark.asyncio
    async def test_scope_isolation_still_applies(self, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_VECTOR_RECALL_ENABLED", True)
        import agentix.retrieval.infra.rag.dense as dense

        monkeypatch.setattr(
            dense,
            "embed_texts_sync",
            lambda texts, model=None, base_url=None: [[1.0, 0.0]],
        )
        mem = InMemoryMemory()
        _seed(mem, _item("他人条目", item_id="other", subject="u2"), [1.0, 0.0])
        hits = await mem.recall(
            scope=MemoryScope.USER.value, subject="u1", query="teleport"
        )
        assert hits == []


# ===========================================================================
# 4. M-P1-2 后台评审 LLM 路由 + turn-end 生产者
# ===========================================================================


class _FakeLLM:
    def __init__(self, content: str = "", boom: bool = False):
        self._content = content
        self._boom = boom

    async def ainvoke(self, messages):
        if self._boom:
            raise RuntimeError("llm down")
        from types import SimpleNamespace

        return SimpleNamespace(content=self._content)


class _RecordingManager:
    def __init__(self):
        self.remembered: list[MemoryItem] = []

    async def remember(self, item: MemoryItem) -> str:
        self.remembered.append(item)
        return item.item_id or "x"


class TestBackgroundReviewLlm:
    @pytest.mark.asyncio
    async def test_llm_route_user_profile(self):
        decision = await route_memory_item_llm(
            _item("长期偏好", item_id="a"), client=_FakeLLM("user_profile")
        )
        assert decision.target == TARGET_PROFILE
        assert decision.reason == "llm-route"

    @pytest.mark.asyncio
    async def test_llm_route_episodic(self):
        decision = await route_memory_item_llm(
            _item("临时任务", item_id="a"), client=_FakeLLM("  episodic  ")
        )
        assert decision.target == TARGET_EPISODIC
        assert decision.reason == "llm-route"

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_rule(self):
        decision = await route_memory_item_llm(
            _item("低置信", item_id="a", confidence=0.2),
            client=_FakeLLM(boom=True),
        )
        expected = route_memory_item(_item("低置信", item_id="a", confidence=0.2))
        assert decision.target == expected.target
        assert decision.reason == f"fallback-rule:{expected.reason}"

    @pytest.mark.asyncio
    async def test_llm_unrecognized_output_falls_back_to_rule(self):
        item = _item("长期偏好", item_id="a", confidence=0.9)
        decision = await route_memory_item_llm(item, client=_FakeLLM("banana"))
        expected = route_memory_item(item)
        assert decision.target == expected.target
        assert decision.reason.startswith("fallback-rule:")

    @pytest.mark.asyncio
    async def test_prompt_contains_content_and_targets(self):
        # prompt 构造是纯字符串操作：client=None 但 ainvoke 不该被触达——
        # 用假 client 捕获 messages 断言内容（同时验证模板字段）。
        captured: list = []

        class _Cap(_FakeLLM):
            async def ainvoke(self, messages):
                captured.extend(messages)
                return await super().ainvoke(messages)

        await route_memory_item_llm(
            _item("用户要求附法条出处", item_id="a"), client=_Cap("episodic")
        )
        sent = captured[0]["content"]
        assert "用户要求附法条出处" in sent
        assert "user_profile" in sent and "episodic" in sent
        assert "{content}" in REVIEW_ROUTE_PROMPT  # 模板持占位符
        assert "{content}" not in sent  # 发出的 prompt 已渲染成真实内容

    @pytest.mark.asyncio
    async def test_review_items_uses_router(self):
        # 规则会判 episodic（semantic 置信 <0.6），router 判 profile ⇒ 晋升
        item = _item("中置信偏好", item_id="mid", confidence=0.55)
        assert route_memory_item(item).target == TARGET_EPISODIC
        mgr = _RecordingManager()

        async def _router(it):
            return RouteDecision(TARGET_PROFILE, "test-route")

        report = await review_items(mgr, [item], router=_router)
        assert report.promoted == 1
        assert report.decisions[0][2] == "test-route"
        assert len(mgr.remembered) == 1

    @pytest.mark.asyncio
    async def test_review_items_router_exception_falls_back_to_rule(self):
        item = _item("低置信", item_id="low", confidence=0.1)
        mgr = _RecordingManager()

        async def _bad(it):
            raise RuntimeError("router boom")

        report = await review_items(mgr, [item], router=_bad)
        assert report.checked == 1
        assert report.promoted == 0  # 规则兜底：低置信不晋升
        assert report.decisions[0][1] == TARGET_EPISODIC

    @pytest.mark.asyncio
    async def test_review_items_without_router_unchanged(self):
        item = MemoryItem(
            content="稳定画像",
            kind=MemoryKind.USER,
            scope=MemoryScope.USER,
            item_id="st",
            subject="u1",
            confidence=0.9,
        )
        mgr = _RecordingManager()
        report = await review_items(mgr, [item])
        assert report.promoted == 1
        assert report.decisions[0][2] == "kind-user"

    @pytest.mark.asyncio
    async def test_schedule_review_use_llm_routes_through_llm(self, monkeypatch):
        SUMMARY = "用户要求回答时必须附上法条出处。本次咨询了劳动合同解除问题。"
        mgr = _RecordingManager()
        seen: list[str] = []

        async def _fake_llm_route(it, **kw):
            seen.append(it.content)
            return RouteDecision(TARGET_EPISODIC, "test-llm")

        monkeypatch.setattr(memory_review, "route_memory_item_llm", _fake_llm_route)
        task = schedule_review_for_summary(
            SUMMARY, manager_factory=lambda: mgr, use_llm=True
        )
        assert task is not None
        report = await task
        assert len(seen) == 1
        assert report.promoted == 0  # LLM 路由判 episodic ⇒ 不晋升
        assert report.decisions[0][2] == "test-llm"

    @pytest.mark.asyncio
    async def test_schedule_review_without_llm_never_touches_router(
        self, monkeypatch
    ):
        SUMMARY = "用户要求回答时必须附上法条出处。本次咨询了劳动合同解除问题。"
        mgr = _RecordingManager()

        async def _boom(it, **kw):
            raise AssertionError("use_llm=False 不该走 LLM 路由")

        monkeypatch.setattr(memory_review, "route_memory_item_llm", _boom)
        task = schedule_review_for_summary(
            SUMMARY, manager_factory=lambda: mgr, use_llm=False
        )
        report = await task
        assert report.promoted == 1  # 规则路由原契约


class TestTurnReviewProducer:
    def _service(self):
        from agentix.application.services.chat_service import ChatService

        # 生产者不依赖任何实例状态：绕过重构造，直接取未初始化实例
        return ChatService.__new__(ChatService)

    def test_flag_off_never_schedules(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(settings, "MEMORY_REVIEW_ENABLED", False)
        monkeypatch.setattr(
            memory_review,
            "schedule_review_for_summary",
            lambda *a, **k: calls.append((a, k)),
        )
        self._service()._maybe_schedule_turn_review(
            question="问", answer="答"
        )
        assert calls == []

    def test_flag_on_schedules_with_turn_text(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(settings, "MEMORY_REVIEW_ENABLED", True)
        monkeypatch.setattr(
            memory_review,
            "schedule_review_for_summary",
            lambda *a, **k: calls.append((a, k)),
        )
        self._service()._maybe_schedule_turn_review(
            question="合同违约金怎么算", answer="按约定优先适用"
        )
        assert len(calls) == 1
        args, kwargs = calls[0]
        assert "合同违约金怎么算" in args[0]
        assert "按约定优先适用" in args[0]
        assert kwargs.get("use_llm") is True

    def test_scheduling_failure_is_swallowed(self, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_REVIEW_ENABLED", True)

        def _boom(*a, **k):
            raise RuntimeError("queue down")

        monkeypatch.setattr(memory_review, "schedule_review_for_summary", _boom)
        # 不抛 = 合同（评审失败只打点不影响主链）
        self._service()._maybe_schedule_turn_review(question="q", answer="a")

    def test_producer_wired_at_three_call_sites(self):
        src = _read("agentix/application/services/chat_service.py")
        # 1 def + 3 调用（ask 成功 / stream 正常 done / stream 兜底 done）
        assert src.count("_maybe_schedule_turn_review(") == 4


# ===========================================================================
# 5. M-P1-5 真分词/真计数
# ===========================================================================


class TestRealTokenCount:
    TEXTS = [
        "",
        "hello world",
        "统一执行链 Stage F1b 真分词计数测试",
        "mixed 中英 mixed 文本 12345 与符号 !@#",
        "x",  # len//4 == 0 的边界
    ]

    @pytest.mark.parametrize("text", TEXTS)
    def test_flag_off_equals_len_div4(self, text, monkeypatch):
        monkeypatch.setattr(settings, "CONTEXT_REAL_TOKEN_COUNT_ENABLED", False)
        assert token_count.count_tokens(text) == len(text) // 4

    @pytest.mark.parametrize("text", TEXTS)
    def test_flag_on_matches_tiktoken(self, text, monkeypatch):
        monkeypatch.setattr(settings, "CONTEXT_REAL_TOKEN_COUNT_ENABLED", True)
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        assert token_count.count_tokens(text) == len(
            enc.encode(text, disallowed_special=())
        )

    def test_flag_on_tokenizer_failure_falls_back_to_cjk(self, monkeypatch):
        monkeypatch.setattr(settings, "CONTEXT_REAL_TOKEN_COUNT_ENABLED", True)
        monkeypatch.setattr(token_count, "_load_encoding", lambda: None)
        text = "你好世界"
        # CJK 启发式按字计 4；len//4 会低估为 1
        assert token_count.count_tokens(text) == 4

    def test_encode_failure_falls_back_to_cjk(self, monkeypatch):
        monkeypatch.setattr(settings, "CONTEXT_REAL_TOKEN_COUNT_ENABLED", True)

        class _BadEnc:
            def encode(self, text, disallowed_special=()):
                raise RuntimeError("encode boom")

        monkeypatch.setattr(token_count, "_load_encoding", lambda: _BadEnc())
        assert token_count.count_tokens("你好世界") == 4

    def test_context_estimation_sites_wired(self):
        engine = _read("agentix/agent/context/engine.py")
        simple = _read("agentix/agent/context/simple.py")
        compaction = _read("agentix/agent/context/compaction.py")
        for src, name in ((engine, "engine"), (simple, "simple"), (compaction, "compaction")):
            assert "count_tokens(" in src, name
        # 7 处旧估点全部退役（flag-off 由 count_tokens 内部保持 len//4 等价）
        assert "len(prompt) // 4" not in engine
        assert "len(prompt) // 4" not in simple
        assert "len(result) // 4" not in compaction

    @pytest.mark.asyncio
    async def test_flag_off_context_build_unchanged(self, monkeypatch):
        """flag-off：SimpleContextBuilder 的 token_estimate 仍按 len//4。"""
        from agentix.agent.context.simple import SimpleContextBuilder
        from agentix.domain.agent.types import TaskSpec

        monkeypatch.setattr(settings, "CONTEXT_REAL_TOKEN_COUNT_ENABLED", False)
        builder = SimpleContextBuilder(rules=["R1"])
        snap = await builder.build(TaskSpec(goal="目标文本 payload"), budget_tokens=0)
        assert snap.token_estimate == max(1, len(snap.prompt) // 4)


# ===========================================================================
# 6. 检索旁路棘轮随迁（p74 equality）
# ===========================================================================


class TestBypassRatchetEntry:
    def test_allowlist_registers_memory_service_dense_import(self):
        src = _read("tests/test_p74_m5_retrieval_unification.py")
        assert '"agentix/agent/memory/service.py"' in src
        assert '"agentix.retrieval.infra.rag.dense"' in src
