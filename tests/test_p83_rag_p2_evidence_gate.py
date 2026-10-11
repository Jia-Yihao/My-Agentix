# -*- coding: utf-8 -*-
"""F2b-a · Stage F2：R-P2-2 证据门禁守卫（rag-optimization §6）。

覆盖：
1. 键同步（rset 声明 / selfcheck 白名单 / .env.example——GROUNDING_* 同族三处）
2. `evidence_gate` 纯函数：模式归一 fail-safe、触发信号、动作判定、警告文案
3. `_post_process_and_persist` 接线（observe 逐位等价 / warn 附警告 / refuse 拒答
   且 citations 清空 / 干净答案 refuse 不触发 / 审计字段落 tool_ctx）
4. Prometheus `evidence_gate_total{action}` 计数
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from agentix.domain.policies.evidence_gate import (
    ACTION_DELIVER,
    ACTION_REFUSE,
    ACTION_WARN,
    EVIDENCE_GATE_REFUSAL_MSG,
    GATE_MODES,
    build_warn_footer,
    decide_evidence_gate,
    normalize_gate_mode,
)
from agentix.retrieval.domain.models.chat import Message
from agentix.retrieval.domain.models.index import Answer, RetrievalRun
from agentix.retrieval.settings import settings as rset_settings

# ===========================================================================
# 1. 键同步
# ===========================================================================


class TestKeySync:
    def test_rset_default_observe(self):
        assert rset_settings.EVIDENCE_GATE_MODE == "observe"

    def test_selfcheck_ignores_missing(self):
        from agentix.config.selfcheck import DEFAULT_IGNORED_MISSING

        assert "EVIDENCE_GATE_MODE" in DEFAULT_IGNORED_MISSING

    def test_env_example_documents_key(self):
        from pathlib import Path

        text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(
            encoding="utf-8"
        )
        assert "EVIDENCE_GATE_MODE=observe" in text

    def test_chat_service_reads_gate_mode(self):
        from pathlib import Path

        text = (
            Path(__file__).resolve().parents[1]
            / "agentix/retrieval/usecases/chat_service.py"
        ).read_text(encoding="utf-8")
        assert "settings.EVIDENCE_GATE_MODE" in text
        assert "evidence_gate import" in text or "evidence_gate" in text


# ===========================================================================
# 2. 纯函数：normalize / decide / footer
# ===========================================================================


class TestNormalizeMode:
    def test_valid_passthrough_case_insensitive(self):
        assert normalize_gate_mode("observe") == "observe"
        assert normalize_gate_mode("WARN") == "warn"
        assert normalize_gate_mode(" Refuse ") == "refuse"

    def test_unknown_fail_safe_to_observe(self):
        assert normalize_gate_mode("block") == "observe"
        assert normalize_gate_mode("") == "observe"
        assert normalize_gate_mode(None) == "observe"
        assert normalize_gate_mode(123) == "observe"


class TestDecideGate:
    def test_observe_mode_delivers_but_records_triggers(self):
        d = decide_evidence_gate(
            mode="observe", unverified_citations=2, stripped_citations=1
        )
        assert d.action == ACTION_DELIVER
        assert d.mode == "observe"
        assert d.triggers == ("citation_unverified", "citation_untraceable")
        assert d.to_dict() == {
            "mode": "observe",
            "action": "deliver",
            "triggers": ["citation_unverified", "citation_untraceable"],
        }

    def test_no_triggers_deliver_in_all_modes(self):
        for mode in GATE_MODES:
            d = decide_evidence_gate(mode=mode, grounding_score=0.9, warn_threshold=0.6)
            assert d.action == ACTION_DELIVER and d.triggers == (), mode

    def test_grounding_low_score_trigger(self):
        d = decide_evidence_gate(
            mode="warn", grounding_score=0.4, warn_threshold=0.6
        )
        assert d.action == ACTION_WARN
        assert d.triggers == ("grounding_low_score",)

    def test_grounding_skipped_or_none_not_triggered(self):
        assert decide_evidence_gate(
            mode="warn", grounding_score=None, warn_threshold=0.6
        ).triggers == ()
        assert decide_evidence_gate(
            mode="warn",
            grounding_score=0.1,
            grounding_skipped_reason="no_evidence",
            warn_threshold=0.6,
        ).triggers == ()

    def test_grounding_threshold_zero_disables_signal(self):
        d = decide_evidence_gate(mode="warn", grounding_score=0.1, warn_threshold=0)
        assert d.action == ACTION_DELIVER and d.triggers == ()

    def test_score_equal_to_threshold_not_triggered(self):
        d = decide_evidence_gate(
            mode="refuse", grounding_score=0.6, warn_threshold=0.6
        )
        assert d.action == ACTION_DELIVER and d.triggers == ()

    def test_refuse_mode_with_triggers(self):
        d = decide_evidence_gate(mode="refuse", unverified_citations=1)
        assert d.action == ACTION_REFUSE
        assert d.triggers == ("citation_unverified",)

    def test_unknown_mode_fails_safe_to_observe(self):
        d = decide_evidence_gate(mode="block-everything", unverified_citations=1)
        assert d.action == ACTION_DELIVER
        assert d.mode == "observe"


class TestWarnFooter:
    def test_footer_lists_all_triggers(self):
        d = decide_evidence_gate(
            mode="warn",
            grounding_score=0.2,
            warn_threshold=0.6,
            unverified_citations=3,
            stripped_citations=1,
        )
        footer = build_warn_footer(d, score=0.2, unverified=3, stripped=1)
        assert footer.startswith("证据核验提示：")
        assert "证据锚定分偏低（0.20）" in footer
        assert "3 条法条引用未通过核验" in footer
        assert "1 条引用无法溯源已移除" in footer

    def test_footer_single_trigger(self):
        d = decide_evidence_gate(mode="warn", unverified_citations=2)
        footer = build_warn_footer(d, unverified=2)
        assert "2 条法条引用未通过核验" in footer
        assert "锚定分" not in footer

    def test_footer_empty_without_triggers(self):
        d = decide_evidence_gate(mode="warn")
        assert build_warn_footer(d) == ""

    def test_footer_distinct_from_r12_annotation_header(self):
        from agentix.retrieval.domain.services.citation_verify import _ANNOTATION_HEADER

        d = decide_evidence_gate(mode="warn", unverified_citations=1)
        footer = build_warn_footer(d, unverified=1)
        assert "引用核验提示" not in footer
        assert "证据核验提示" in footer
        assert "引用核验提示" in _ANNOTATION_HEADER


# ===========================================================================
# 3. 接线：_post_process_and_persist
# ===========================================================================

_CLEAN_CHUNKS = [
    {
        "chunk_id": "c1",
        "content": "根据劳动合同法，试用期最长不超过六个月。",
        "source": "劳动合同法",
    }
]
_UNRELATED_CHUNKS = [{"chunk_id": "c2", "content": "天气预报显示明天晴。", "source": "气象"}]


def _ctx(*, citations=None, chunks=None) -> SimpleNamespace:
    return SimpleNamespace(
        last_citations=list(citations or []),
        last_hit_chunks=list(chunks or []),
        last_contexts=[],
        last_grounding={},
    )


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(
        engine, tables=[Answer.__table__, RetrievalRun.__table__, Message.__table__]
    )
    with Session(engine) as session:
        yield session


async def _run(session: Session, *, answer_text: str, tool_ctx):
    from agentix.retrieval.usecases.chat_service import _post_process_and_persist

    user_msg = Message(conversation_id="c1", role="user", content="试用期多久？")
    session.add(user_msg)
    session.commit()
    session.refresh(user_msg)
    return await _post_process_and_persist(
        session,
        kb_id="kb1",
        cid="c1",
        user_msg=user_msg,
        question="试用期多久？",
        search_k=5,
        model_name="m",
        answer_text=answer_text,
        tool_ctx=tool_ctx,
    )


@pytest.fixture()
def gate_settings(monkeypatch):
    monkeypatch.setattr(rset_settings, "OUTPUT_FILTER_ENABLED", False)
    monkeypatch.setattr(rset_settings, "EFFECTIVENESS_ASOF_ANNOTATION", False)
    monkeypatch.setattr(rset_settings, "GROUNDING_VALIDATOR_ENABLED", True)
    monkeypatch.setattr(rset_settings, "GROUNDING_LOW_SCORE_WARN", 0.6)
    monkeypatch.setattr(rset_settings, "EVIDENCE_GATE_MODE", "observe")
    return monkeypatch


class TestPostProcessWiring:
    @pytest.mark.asyncio
    async def test_observe_delivers_and_records_triggers(self, db_session, gate_settings):
        answer_text = "依据《虚构法》第九条，试用期为六个月。"
        ctx = _ctx(chunks=_CLEAN_CHUNKS)
        answer, _msg, citations = await _run(
            db_session, answer_text=answer_text, tool_ctx=ctx
        )
        assert EVIDENCE_GATE_REFUSAL_MSG not in answer.answer_text
        assert "证据核验提示" not in answer.answer_text
        assert "引用核验提示" in answer.answer_text, "R1-2 基线标注仍照常运行"
        assert ctx.last_evidence_gate["action"] == ACTION_DELIVER
        assert "citation_unverified" in ctx.last_evidence_gate["triggers"]
        assert citations == [], "无检索命中可溯源引用 ⇒ 空集（基线行为）"

    @pytest.mark.asyncio
    async def test_warn_appends_footer_keeps_answer(
        self, db_session, gate_settings
    ):
        gate_settings.setattr(rset_settings, "EVIDENCE_GATE_MODE", "warn")
        answer_text = "依据《虚构法》第九条，试用期为六个月。"
        ctx = _ctx(chunks=_CLEAN_CHUNKS)
        answer, _msg, _citations = await _run(
            db_session, answer_text=answer_text, tool_ctx=ctx
        )
        assert answer.answer_text.startswith(answer_text)
        assert "证据核验提示：" in answer.answer_text
        assert "法条引用未通过核验" in answer.answer_text
        # 警告必须在正文之后（尾部）
        assert answer.answer_text.index("证据核验提示") > answer.answer_text.index(
            "试用期为六个月"
        )
        assert ctx.last_evidence_gate["action"] == ACTION_WARN

    @pytest.mark.asyncio
    async def test_refuse_replaces_answer_and_clears_citations(
        self, db_session, gate_settings
    ):
        gate_settings.setattr(rset_settings, "EVIDENCE_GATE_MODE", "refuse")
        answer_text = "依据《虚构法》第九条，试用期为六个月。"
        ctx = _ctx(chunks=_CLEAN_CHUNKS)
        answer, msg, citations = await _run(
            db_session, answer_text=answer_text, tool_ctx=ctx
        )
        assert answer.answer_text == EVIDENCE_GATE_REFUSAL_MSG
        assert msg.content == EVIDENCE_GATE_REFUSAL_MSG
        assert citations == []
        assert answer.citations == []
        assert ctx.last_evidence_gate["action"] == ACTION_REFUSE

    @pytest.mark.asyncio
    async def test_refuse_clean_answer_delivers_normally(
        self, db_session, gate_settings
    ):
        gate_settings.setattr(rset_settings, "EVIDENCE_GATE_MODE", "refuse")
        answer_text = "根据劳动合同法，试用期最长不超过六个月。"
        ctx = _ctx(chunks=_CLEAN_CHUNKS)
        answer, _msg, _citations = await _run(
            db_session, answer_text=answer_text, tool_ctx=ctx
        )
        assert EVIDENCE_GATE_REFUSAL_MSG not in answer.answer_text
        assert answer.answer_text.startswith(answer_text)
        assert ctx.last_evidence_gate == {
            "mode": "refuse",
            "action": ACTION_DELIVER,
            "triggers": [],
        }

    @pytest.mark.asyncio
    async def test_warn_grounding_low_score_only(self, db_session, gate_settings):
        gate_settings.setattr(rset_settings, "EVIDENCE_GATE_MODE", "warn")
        answer_text = "北极星量子隧穿合同的违约金上限是百分之五百。"
        ctx = _ctx(chunks=_UNRELATED_CHUNKS)
        answer, _msg, _citations = await _run(
            db_session, answer_text=answer_text, tool_ctx=ctx
        )
        assert answer.answer_text.startswith(answer_text)
        assert "证据核验提示：" in answer.answer_text
        assert "证据锚定分偏低" in answer.answer_text
        assert "法条引用" not in answer.answer_text.split("证据核验提示：")[-1]
        assert ctx.last_evidence_gate["triggers"] == ["grounding_low_score"]
        assert ctx.last_grounding["score"] is not None
        assert ctx.last_grounding["score"] < 0.6

    @pytest.mark.asyncio
    async def test_observe_grounding_low_still_delivers(self, db_session, gate_settings):
        answer_text = "北极星量子隧穿合同的违约金上限是百分之五百。"
        ctx = _ctx(chunks=_UNRELATED_CHUNKS)
        answer, _msg, _citations = await _run(
            db_session, answer_text=answer_text, tool_ctx=ctx
        )
        assert answer.answer_text.startswith(answer_text)
        assert "证据核验提示" not in answer.answer_text
        assert ctx.last_evidence_gate["action"] == ACTION_DELIVER
        assert ctx.last_evidence_gate["triggers"] == ["grounding_low_score"]


# ===========================================================================
# 4. Prometheus 计数
# ===========================================================================


class TestMetrics:
    def test_record_evidence_gate_increments_counter(self):
        from agentix.observability.metrics import record_evidence_gate
        from prometheus_client import REGISTRY

        def _get(action: str) -> float:
            return (
                REGISTRY.get_sample_value(
                    "evidence_gate_total", {"action": action}
                )
                or 0.0
            )

        before_warn = _get("warn")
        record_evidence_gate("warn")
        assert _get("warn") == before_warn + 1
        before_refuse = _get("refuse")
        record_evidence_gate("refuse")
        assert _get("refuse") == before_refuse + 1
