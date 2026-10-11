# -*- coding: utf-8 -*-
"""F2b-b · Stage F2：R-P2-1 查询改写/HyDE 守卫（rag-optimization §6）。

覆盖：
1. 键同步（rset 默认 false ×2 / .env.example 注释示例行）
2. `query_rewrite` 归一纯函数与 fail-open 语义
3. `retrieve_scoped_docs_cached` 接线（flag-off 原查询、flag-on 生效改写/HyDE、
   回退与异常 fail-open、缓存池后缀隔离、history 透传）
4. 预检路径 history 接线（源级）
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import agentix.retrieval.usecases.chat_service as chat_service_mod
from agentix.retrieval.settings import settings as rset_settings
from agentix.retrieval.usecases import query_rewrite
from agentix.retrieval.usecases.chat_service import (
    _normalize_question,
    retrieve_scoped_docs_cached,
)

_QUESTION = "竞业限制补偿标准是什么？"


class FakeLLM:
    """同步 invoke（invoke_with_breaker 走 to_thread 调它）。"""

    def __init__(self, content: str = "", exc: Exception | None = None):
        self.content = content
        self.exc = exc
        self.prompts: list = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(content=self.content)


class FakeRedis:
    def __init__(self):
        self.get_keys: list[str] = []
        self.set_calls: list[tuple] = []

    def get(self, key):
        self.get_keys.append(key)
        return None

    def set(self, key, value, ex=None, nx=False):
        self.set_calls.append((key, value, ex, nx))


@pytest.fixture()
def flags(monkeypatch):
    monkeypatch.setattr(rset_settings, "RETRIEVAL_QUERY_REWRITE_ENABLED", False)
    monkeypatch.setattr(rset_settings, "RETRIEVAL_HYDE_ENABLED", False)
    monkeypatch.setattr(rset_settings, "RETRIEVAL_HYBRID_ENABLED", False)
    monkeypatch.setattr(rset_settings, "LLM_CIRCUIT_BREAKER_ENABLED", False)
    return monkeypatch


# ===========================================================================
# 1. 键同步
# ===========================================================================


class TestKeySync:
    def test_rset_defaults_off(self):
        assert rset_settings.RETRIEVAL_QUERY_REWRITE_ENABLED is False
        assert rset_settings.RETRIEVAL_HYDE_ENABLED is False

    def test_env_example_documents_keys(self):
        from pathlib import Path

        text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(
            encoding="utf-8"
        )
        assert "# RETRIEVAL_QUERY_REWRITE_ENABLED=false" in text
        assert "# RETRIEVAL_HYDE_ENABLED=false" in text

    def test_rewire_read_in_single_seam(self):
        from pathlib import Path

        text = Path(
            chat_service_mod.__file__
        ).read_text(encoding="utf-8")
        assert "settings.RETRIEVAL_QUERY_REWRITE_ENABLED" in text
        assert "settings.RETRIEVAL_HYDE_ENABLED" in text
        assert "cache_suffix" in text


# ===========================================================================
# 2. 归一纯函数
# ===========================================================================


class TestNormalizeQueryOutput:
    def test_empty_falls_back(self):
        assert query_rewrite.normalize_query_output("", "q") == ("q", False)
        assert query_rewrite.normalize_query_output("   \n ", "q") == ("q", False)
        assert query_rewrite.normalize_query_output(None, "q") == ("q", False)

    def test_takes_first_line_and_strips_quotes(self):
        text, applied = query_rewrite.normalize_query_output(
            '  「合同法 试用期」 \n这是解释文字\n', "原问题"
        )
        assert applied and text == "合同法 试用期"

    def test_same_query_not_applied(self):
        assert query_rewrite.normalize_query_output(_QUESTION, _QUESTION) == (
            _QUESTION,
            False,
        )
        spaced = "竞业限制 补偿标准 是什么？"
        assert query_rewrite.normalize_query_output(spaced, _QUESTION) == (
            _QUESTION,
            False,
        )

    def test_long_output_truncated(self):
        long = "法" * 600
        text, applied = query_rewrite.normalize_query_output(long, "q")
        assert applied and len(text) == query_rewrite._MAX_QUERY_CHARS

    def test_normal_applied(self):
        text, applied = query_rewrite.normalize_query_output(
            "劳动合同法 试用期上限", _QUESTION
        )
        assert applied and text == "劳动合同法 试用期上限"


class TestNormalizeDocumentOutput:
    def test_empty_falls_back(self):
        assert query_rewrite.normalize_document_output("", "q") == ("q", False)

    def test_keeps_multiline_doc_and_truncates(self):
        doc = "第1行内容\n" + "文" * 1300
        text, applied = query_rewrite.normalize_document_output(doc, "q")
        assert applied
        assert text.startswith("第1行内容")
        assert len(text) == query_rewrite._MAX_DOC_CHARS

    def test_same_query_not_applied(self):
        assert query_rewrite.normalize_document_output(_QUESTION, _QUESTION) == (
            _QUESTION,
            False,
        )


# ===========================================================================
# 3. LLM 薄封装（fail-open）
# ===========================================================================


class TestLLMWrappers:
    @pytest.mark.asyncio
    async def test_rewrite_success_passes_history(self):
        llm = FakeLLM(content="竞业限制补偿 计算标准")
        text, applied = await query_rewrite.rewrite_query(
            question=_QUESTION, history="用户上轮：公司要裁我", llm=llm
        )
        assert applied and text == "竞业限制补偿 计算标准"
        assert len(llm.prompts) == 1
        prompt_text = llm.prompts[0][0]["content"]
        assert "公司要裁我" in prompt_text
        assert _QUESTION in prompt_text

    @pytest.mark.asyncio
    async def test_rewrite_llm_exception_fail_open(self):
        llm = FakeLLM(exc=RuntimeError("boom"))
        text, applied = await query_rewrite.rewrite_query(
            question=_QUESTION, llm=llm
        )
        assert (text, applied) == (_QUESTION, False)

    @pytest.mark.asyncio
    async def test_rewrite_empty_history_placeholder(self):
        llm = FakeLLM(content="新查询")
        await query_rewrite.rewrite_query(question=_QUESTION, history="", llm=llm)
        assert "（无）" in llm.prompts[0][0]["content"]

    @pytest.mark.asyncio
    async def test_rewrite_timeout_fail_open(self, monkeypatch):
        monkeypatch.setattr(query_rewrite, "_REWRITE_TIMEOUT_S", 0.01)

        class SlowLLM:
            def invoke(self, prompt):
                import time

                time.sleep(0.05)
                return SimpleNamespace(content="太慢了")

        text, applied = await query_rewrite.rewrite_query(
            question=_QUESTION, llm=SlowLLM()
        )
        assert (text, applied) == (_QUESTION, False)

    @pytest.mark.asyncio
    async def test_rewrite_empty_question_short_circuits(self):
        llm = FakeLLM(content="x")
        assert await query_rewrite.rewrite_query(question="  ", llm=llm) == (
            "  ",
            False,
        )
        assert llm.prompts == []

    @pytest.mark.asyncio
    async def test_hyde_success_and_fail_open(self):
        llm = FakeLLM(content="试用期不得超过六个月，超过部分无效……")
        text, applied = await query_rewrite.hyde_expand(
            question=_QUESTION, llm=llm
        )
        assert applied and "试用期" in text
        assert _QUESTION in llm.prompts[0][0]["content"]

        bad = FakeLLM(exc=RuntimeError("boom"))
        assert await query_rewrite.hyde_expand(question=_QUESTION, llm=bad) == (
            _QUESTION,
            False,
        )


# ===========================================================================
# 4. 接线：retrieve_scoped_docs_cached
# ===========================================================================


def _install_recorder(monkeypatch, docs=None):
    """把真实向量检索换成记录器（离线）。返回 (records, docs) 记录列表。"""
    from langchain_core.documents import Document

    records: list[dict] = []
    out_docs = (
        docs
        if docs is not None
        else [Document(page_content="竞业限制补偿月标准", metadata={"chunk_id": "c1"})]
    )

    def fake_retrieve(vectorstore, q, *, search_k, metadata_filter):
        records.append({"query": q, "search_k": search_k})
        return list(out_docs)

    monkeypatch.setattr(chat_service_mod, "retrieve_docs_with_retry", fake_retrieve)
    return records


async def _run_retrieve(*, question=_QUESTION, cache_redis=None, history=""):
    return await retrieve_scoped_docs_cached(
        kb_id="kb1",
        question=question,
        search_k=5,
        metadata_filter=None,
        vectorstore=object(),
        cache_redis=cache_redis,
        artifact_id="a1",
        scope_token="",
        session=None,
        history=history,
    )


class TestRewriteWiring:
    @pytest.mark.asyncio
    async def test_flags_off_never_calls_transform(self, flags, monkeypatch):
        records = _install_recorder(monkeypatch)

        async def _boom(**kwargs):  # pragma: no cover - 不应被调用
            raise AssertionError("transform must not run when flags off")

        monkeypatch.setattr(query_rewrite, "rewrite_query", _boom)
        monkeypatch.setattr(query_rewrite, "hyde_expand", _boom)
        await _run_retrieve()
        assert records == [{"query": _normalize_question(_QUESTION), "search_k": 5}]

    @pytest.mark.asyncio
    async def test_rewrite_on_uses_effective_query(self, flags, monkeypatch):
        flags.setattr(rset_settings, "RETRIEVAL_QUERY_REWRITE_ENABLED", True)
        records = _install_recorder(monkeypatch)
        seen = {}

        async def fake_rewrite(**kwargs):
            seen.update(kwargs)
            return "竞业限制 补偿 标准 法条", True

        monkeypatch.setattr(query_rewrite, "rewrite_query", fake_rewrite)
        await _run_retrieve(history="上轮：公司要裁我")
        assert records[0]["query"] == _normalize_question(
            "竞业限制 补偿 标准 法条"
        )
        assert seen["history"] == "上轮：公司要裁我"

    @pytest.mark.asyncio
    async def test_rewrite_fallback_keeps_original_query(self, flags, monkeypatch):
        flags.setattr(rset_settings, "RETRIEVAL_QUERY_REWRITE_ENABLED", True)
        records = _install_recorder(monkeypatch)

        async def fake_rewrite(**kwargs):
            return kwargs["question"], False

        monkeypatch.setattr(query_rewrite, "rewrite_query", fake_rewrite)
        await _run_retrieve()
        assert records[0]["query"] == _normalize_question(_QUESTION)

    @pytest.mark.asyncio
    async def test_rewrite_exception_fail_open(self, flags, monkeypatch):
        flags.setattr(rset_settings, "RETRIEVAL_QUERY_REWRITE_ENABLED", True)
        records = _install_recorder(monkeypatch)

        async def fake_rewrite(**kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(query_rewrite, "rewrite_query", fake_rewrite)
        await _run_retrieve()
        assert records[0]["query"] == _normalize_question(_QUESTION)

    @pytest.mark.asyncio
    async def test_hyde_on_uses_hypothetical_doc(self, flags, monkeypatch):
        flags.setattr(rset_settings, "RETRIEVAL_HYDE_ENABLED", True)
        records = _install_recorder(monkeypatch)

        async def fake_hyde(**kwargs):
            return "假设性文档：试用期上限六个月", True

        monkeypatch.setattr(query_rewrite, "hyde_expand", fake_hyde)
        await _run_retrieve()
        assert records[0]["query"] == _normalize_question(
            "假设性文档：试用期上限六个月"
        )

    @pytest.mark.asyncio
    async def test_both_flags_compose_rewrite_then_hyde(self, flags, monkeypatch):
        flags.setattr(rset_settings, "RETRIEVAL_QUERY_REWRITE_ENABLED", True)
        flags.setattr(rset_settings, "RETRIEVAL_HYDE_ENABLED", True)
        records = _install_recorder(monkeypatch)
        order = []

        async def fake_rewrite(**kwargs):
            order.append("rewrite")
            return "改写后的查询", True

        async def fake_hyde(**kwargs):
            order.append("hyde")
            assert kwargs["question"] == "改写后的查询"
            return "HyDE 文档正文", True

        monkeypatch.setattr(query_rewrite, "rewrite_query", fake_rewrite)
        monkeypatch.setattr(query_rewrite, "hyde_expand", fake_hyde)
        await _run_retrieve()
        assert order == ["rewrite", "hyde"]
        assert records[0]["query"] == _normalize_question("HyDE 文档正文")

    @pytest.mark.asyncio
    async def test_cache_pool_isolated_by_suffix(self, flags, monkeypatch):
        flags.setattr(rset_settings, "RETRIEVAL_QUERY_REWRITE_ENABLED", True)
        _install_recorder(monkeypatch)

        async def fake_rewrite(**kwargs):
            return "改写后的查询", True

        monkeypatch.setattr(query_rewrite, "rewrite_query", fake_rewrite)
        fake_redis = FakeRedis()
        await _run_retrieve(cache_redis=fake_redis)
        assert fake_redis.get_keys and fake_redis.get_keys[0].endswith("|r1")
        assert fake_redis.set_calls and fake_redis.set_calls[0][0].endswith("|r1")

    @pytest.mark.asyncio
    async def test_cache_pool_same_as_baseline_when_fallback(
        self, flags, monkeypatch
    ):
        flags.setattr(rset_settings, "RETRIEVAL_QUERY_REWRITE_ENABLED", True)
        _install_recorder(monkeypatch)

        async def fake_rewrite(**kwargs):
            return kwargs["question"], False

        monkeypatch.setattr(query_rewrite, "rewrite_query", fake_rewrite)
        fake_redis = FakeRedis()
        await _run_retrieve(cache_redis=fake_redis)
        assert fake_redis.get_keys
        assert not fake_redis.get_keys[0].endswith("|r1")

    @pytest.mark.asyncio
    async def test_baseline_cache_key_has_no_suffix(self, flags, monkeypatch):
        _install_recorder(monkeypatch)
        fake_redis = FakeRedis()
        await _run_retrieve(cache_redis=fake_redis)
        assert fake_redis.get_keys
        key = fake_redis.get_keys[0]
        assert "|r1" not in key and "|y1" not in key and "|h1" not in key


class TestHistoryThreading:
    def test_preflight_forwards_history(self):
        from pathlib import Path

        helpers = Path(chat_service_mod.__file__).with_name(
            "chat_service_helpers.py"
        ).read_text(encoding="utf-8")
        assert "history=history_text" in helpers
        main = Path(chat_service_mod.__file__).read_text(encoding="utf-8")
        assert "history_text=history_text" in main
