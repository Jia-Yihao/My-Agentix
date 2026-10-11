"""R3-5 Context Engine 测试（架构文档 §24-28 + 指南 v2.0 L1⑦）。

覆盖：
① 静态前缀版本化（版本号确定性 + 不变性）
② token 估算（CJK / ASCII）
③ authority 排序（五档、别名、数字、未知 fail-open、稳定排序）
④ Context Budget（不限额 / 截断 / 丢弃）
⑤ 大文档隔离（外置句柄 / 降级内联 / 关闭）
⑥ assemble_context 管线 meta
⑦ 状态栏字段
⑧ build_prompt 兼容：不传 status_fields 输出不变；传了则注入状态栏
"""

from __future__ import annotations

import hashlib

from langchain_core.documents import Document as LCDocument


# ---------- 工具 ----------

def _doc(text: str, **meta) -> LCDocument:
    return LCDocument(page_content=text, metadata=meta or {})


# ---------- ① 静态前缀版本化 ----------

def test_static_prefix_version_is_deterministic():
    from agentix.observability.versions import content_version
    from agentix.retrieval.domain.services.context_engine import STATIC_PREFIX, STATIC_PREFIX_VERSION

    expected = "ctx-" + hashlib.sha256(STATIC_PREFIX.encode("utf-8")).hexdigest()[:12]
    assert STATIC_PREFIX_VERSION == expected
    # 二次求值一致（模块常量 + content_version 确定性）
    assert content_version(STATIC_PREFIX, prefix="ctx-") == STATIC_PREFIX_VERSION


def test_static_prefix_invariance_across_calls():
    """不变性：同一会话内多次求值版本号恒等（eval/Prompt 回放归因前提）。"""
    from agentix.retrieval.domain.services.context_engine import STATIC_PREFIX_VERSION as v1
    from agentix.retrieval.domain.services.context_engine import STATIC_PREFIX_VERSION as v2

    assert v1 == v2


# ---------- ② token 估算 ----------

def test_estimate_tokens_cjk_and_ascii():
    from agentix.retrieval.domain.services.context_engine import estimate_tokens

    assert estimate_tokens("") == 0
    # 纯 CJK：1 字 1 token
    assert estimate_tokens("合同法条文") == 5
    # 纯 ASCII：4 字符 1 token
    assert estimate_tokens("abcdefgh") == 2
    # 混合：CJK 逐字 + ASCII 向上取整
    assert estimate_tokens("合同 abc") == 2 + 1


# ---------- ③ authority 排序 ----------

def test_authority_tier_mapping_full_chain():
    from agentix.retrieval.domain.services.context_engine import authority_tier_of

    assert authority_tier_of({"source_authority_level": "法律"}) == 5
    assert authority_tier_of({"source_authority_level": "司法解释"}) == 4
    assert authority_tier_of({"source_authority_level": "法院案例"}) == 3
    assert authority_tier_of({"source_authority_level": "二级资料"}) == 2
    assert authority_tier_of({"source_authority_level": "普通网页"}) == 1
    # 入库侧别名（legal_metadata 的 alias 集合）
    assert authority_tier_of({"authority_level": "law"}) == 5
    assert authority_tier_of({"来源权威等级": "案例"}) == 3
    # 数字直传（含 clamp）
    assert authority_tier_of({"source_authority_level": 4}) == 4
    assert authority_tier_of({"source_authority_level": 9}) == 5
    assert authority_tier_of({"source_authority_level": "3"}) == 3
    # fail-open：未知 / 缺失 / 空
    assert authority_tier_of({"source_authority_level": "神秘来源"}) == 0
    assert authority_tier_of({}) == 0
    assert authority_tier_of(None) == 0


def test_rank_docs_by_authority_stable_and_tiered():
    from agentix.retrieval.domain.services.context_engine import rank_docs_by_authority

    docs = [
        _doc("web", source_authority_level="普通网页"),
        _doc("unknown-a"),                      # 档位 0
        _doc("statute", source_authority_level="法律"),
        _doc("unknown-b"),                      # 档位 0：与 unknown-a 保持相对顺序
        _doc("case", source_authority_level="法院案例"),
        _doc("statute-2", source_authority_level=5),  # 同档：保持检索分序
    ]
    ranked = rank_docs_by_authority(docs)
    assert [d.page_content for d in ranked] == [
        "statute", "statute-2", "case", "web", "unknown-a", "unknown-b",
    ]


# ---------- ④ Context Budget ----------

def test_budget_unlimited_by_default():
    from agentix.retrieval.domain.services.context_engine import apply_context_budget

    docs = [_doc("a" * 100), _doc("合同条款")]
    kept, stats = apply_context_budget(docs, budget_tokens=0)
    assert stats["budget_tokens"] == 0
    assert stats["kept"] == 2 and stats["dropped"] == 0 and stats["truncated"] == 0
    assert [t for _, t, _ in kept] == [d.page_content for d in docs]
    assert all(not tr for _, _, tr in kept)


def test_budget_truncates_and_drops():
    from agentix.retrieval.domain.services.context_engine import apply_context_budget, estimate_tokens

    d1 = _doc("甲" * 50)                 # 50 tokens
    d2 = _doc("乙" * 30)                 # 30 tokens
    d3 = _doc("丙" * 40)                 # 40 tokens
    kept, stats = apply_context_budget([d1, d2, d3], budget_tokens=70)
    # d1 全留（剩 20），d2 截断到 ≤20，d3 丢弃
    assert len(kept) == 2
    assert kept[0] == (d1, d1.page_content, False)
    text2 = kept[1][1]
    assert kept[1][2] is True and len(text2) < 30
    assert estimate_tokens(text2) <= 20
    assert stats["truncated"] == 1 and stats["dropped"] == 1 and stats["kept"] == 2


def test_budget_tiny_budget_keeps_one_char():
    from agentix.retrieval.domain.services.context_engine import apply_context_budget

    d1 = _doc("甲" * 50)
    kept, stats = apply_context_budget([d1], budget_tokens=1)
    assert len(kept) == 1 and kept[0][2] is True
    assert len(kept[0][1]) >= 1
    assert stats["truncated"] == 1


# ---------- ⑤ 大文档隔离 ----------

def test_isolate_disabled_by_threshold_zero():
    from agentix.retrieval.domain.services.context_engine import isolate_oversize_docs

    docs = [_doc("x" * 5000)]
    out, records = isolate_oversize_docs(docs, isolate_threshold=0)
    assert out[0][1] == "x" * 5000 and out[0][2] is False
    assert records == []


def test_isolate_offloads_oversize_doc():
    from agentix.application.services.artifact_store import get_artifact_store, reset_artifact_store
    from agentix.retrieval.domain.services.context_engine import isolate_oversize_docs

    reset_artifact_store()
    try:
        big = "法" * 6000
        docs = [_doc(big, source="民法典", chunk_id="c1"), _doc("短片段")]
        out, records = isolate_oversize_docs(docs, isolate_threshold=2000)
        # 长文档外置：prompt 文本 = 预览 + 句柄标记，原文不在其中
        text0 = out[0][1]
        assert out[0][2] is True
        assert big not in text0
        assert "artifact_id=" in text0 and "重新检索" in text0
        assert records[0]["artifact_id"] and records[0]["chunk_id"] == "c1"
        # 全文可从 artifact store 取回
        assert get_artifact_store().get(records[0]["artifact_id"]) == big
        # 短文档原样
        assert out[1][1] == "短片段" and out[1][2] is False and len(records) == 1
    finally:
        reset_artifact_store()


# ---------- ⑥ assemble_context 管线 ----------

def test_assemble_context_pipeline_meta_and_ordering():
    from agentix.retrieval.domain.services.context_engine import assemble_context

    docs = [
        _doc("网页内容", source_authority_level="普通网页"),
        _doc("法律条文", source_authority_level="法律"),
    ]
    context, meta = assemble_context(docs, budget_tokens=0, isolate_threshold=0)
    # authority 排序生效：法律条文在前
    assert context.index("法律条文") < context.index("网页内容")
    assert meta["input_docs"] == 2 and meta["final_docs"] == 2
    assert meta["authority_tiers"] == [5, 1]
    assert meta["budget"]["kept"] == 2
    assert meta["context_prefix_version"].startswith("ctx-")


def test_assemble_context_ranking_disabled_preserves_order():
    from agentix.retrieval.domain.services.context_engine import assemble_context

    docs = [_doc("first", source_authority_level="普通网页"), _doc("second", source_authority_level="法律")]
    context, meta = assemble_context(docs, ranking_enabled=False)
    assert context == "first\n\nsecond"
    assert "authority_tiers" not in meta


def test_assemble_context_empty():
    from agentix.retrieval.domain.services.context_engine import assemble_context

    context, meta = assemble_context([])
    assert context == "" and meta["input_docs"] == 0


def test_assemble_context_budget_drops_low_tier():
    from agentix.retrieval.domain.services.context_engine import assemble_context

    docs = [
        _doc("法" * 80, source_authority_level="法律"),   # 80 tokens
        _doc("网" * 80, source_authority_level="普通网页"),  # 排序在后，预算不够被丢
    ]
    context, meta = assemble_context(docs, budget_tokens=100)
    assert "法" * 80 in context
    assert "网" * 80 not in context  # 第二条超预算：只保留截断片段，不是全文
    assert meta["budget"]["truncated"] == 1 and meta["budget"]["dropped"] == 0


# ---------- ⑦ 状态栏 ----------

def test_build_status_bar_fields_and_skips_empty():
    from agentix.retrieval.domain.services.context_engine import build_status_bar

    line = build_status_bar(scope="kb", docs=12, retrieved=5, case_stage=None, note="", tags=[])
    assert line == "【对话状态】scope=kb | docs=12 | retrieved=5"
    assert build_status_bar() == ""


# ---------- ⑧ build_prompt 兼容 ----------

def test_build_prompt_unchanged_without_status_fields(monkeypatch):
    from agentix.config.settings import settings as main_settings
    from agentix.retrieval.usecases.chat_service import build_prompt

    monkeypatch.setattr(main_settings, "PROMPT_INJECTION_DETECTION_ENABLED", False)
    kwargs = dict(history_text="历史", context="文档正文", question="问题")
    assert build_prompt(**kwargs) == build_prompt(status_fields=None, **kwargs)
    assert build_prompt(**kwargs) == build_prompt(status_fields={}, **kwargs)
    prompt = build_prompt(**kwargs)
    assert "【对话状态】" not in prompt and "文档正文" in prompt


def test_build_prompt_injects_status_bar(monkeypatch):
    from agentix.config.settings import settings as main_settings
    from agentix.retrieval.usecases.chat_service import build_prompt

    monkeypatch.setattr(main_settings, "PROMPT_INJECTION_DETECTION_ENABLED", False)
    prompt = build_prompt(
        "历史",
        "文档正文",
        "问题",
        status_fields={"scope": "kb:法律库", "retrieved": 5, "case_stage": "一审"},
    )
    assert "【对话状态】scope=kb:法律库 | retrieved=5 | case_stage=一审" in prompt
    assert prompt.index("相关文档") < prompt.index("【对话状态】")


def test_build_prompt_status_bar_after_format_position(monkeypatch):
    """状态栏在最终 prompt 里不被 .format() 吃掉（无花括号冲突）。"""
    from agentix.config.settings import settings as main_settings
    from agentix.retrieval.usecases.chat_service import build_prompt

    monkeypatch.setattr(main_settings, "PROMPT_INJECTION_DETECTION_ENABLED", False)
    prompt = build_prompt("", "ctx", "q", status_fields={"scope": "kb"})
    assert prompt.endswith("【对话状态】scope=kb\n")


# ---------- 接线：query_knowledge_base 使用引擎（轻量冒烟） ----------

def test_agent_tool_context_has_last_context_meta():
    from agentix.retrieval.usecases.chat_agent_tools import AgentToolContext

    assert "last_context_meta" in AgentToolContext.__dataclass_fields__
