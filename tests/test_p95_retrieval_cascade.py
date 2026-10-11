"""Phase H·W2 检索优化验收（p95）。

覆盖规划 §4 W2 前四项：
1. SingleFlight 在飞合并：并发同参 miss 只跑一次、异常广播、key 生命周期、
   flag-off 仍并发各跑（基线）、接线后错误传播且不残留 key；
2. 自适应级联：确定性分类（statute/multi_hop/general）、分数闸升级/过闸、
   单级异常 fail-open、耗尽回退历史最优、chat_service 接线（dense 前置 /
   multi_hop 融合前置 / 闸关即停）；
3. 缓存键升级：租户分池 + 融合指纹 + ``|k2`` 版本标记（后缀次序不破既有
   endswith 断言）、租户解析 fail-open、端口适配显式透传 tenant_id；
4. 精排动态跳过：flag-off 恒不跳、候选少/相似度高分跳过、距离口径保守不跳、
   hybrid stage 留痕 ``applied=false + skipped``、两工具站点接线在位。

运行：./.venv/Scripts/python.exe -m pytest tests/test_p95_retrieval_cascade.py -q
"""
import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

from agentix.infrastructure.cache.singleflight import (
    SingleFlight,
    get_singleflight,
    reset_singleflight,
)
from agentix.retrieval.domain.services.cascade import (
    CascadeLevel,
    classify_query_kind,
    normalize_level_score,
    run_cascade,
)
from agentix.retrieval.settings import settings as rs


@pytest.fixture(autouse=True)
def _clean_singleflight():
    reset_singleflight()
    yield
    reset_singleflight()


def _doc(text: str, score=None) -> Document:
    meta = {"chunk_id": text}
    if score is not None:
        meta["_retrieval_score"] = float(score)
    return Document(page_content=text, metadata=meta)


async def _retrieve(
    *, question="合同违约责任", session=None, cache_redis=None, **kw
):
    from agentix.retrieval.usecases.chat_service import (
        retrieve_scoped_docs_cached,
    )

    return await retrieve_scoped_docs_cached(
        kb_id="kb1",
        question=question,
        search_k=kw.pop("search_k", 5),
        metadata_filter=None,
        vectorstore=object(),
        cache_redis=cache_redis,
        artifact_id="a1",
        scope_token="",
        session=session,
        **kw,
    )


# ===========================================================================
# 1. SingleFlight 原语
# ===========================================================================


class TestSingleFlightPrimitive:
    def test_defaults_off(self):
        assert rs.RETRIEVAL_SINGLEFLIGHT_ENABLED is False
        assert rs.RETRIEVAL_ADAPTIVE_ENABLED is False
        assert rs.RETRIEVAL_CASCADE_MIN_SCORE == 0.0
        assert rs.RERANK_DYNAMIC_SKIP_ENABLED is False

    @pytest.mark.asyncio
    async def test_concurrent_same_key_runs_factory_once(self):
        sf = SingleFlight()
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return {"v": calls}

        results = await asyncio.gather(*[sf.do("k", factory) for _ in range(6)])
        assert calls == 1
        assert all(r is results[0] for r in results)
        assert sf.inflight_count == 0

    @pytest.mark.asyncio
    async def test_different_keys_run_in_parallel(self):
        sf = SingleFlight()
        calls = []

        def factory(key):
            async def _run():
                calls.append(key)
                await asyncio.sleep(0.01)
                return key

            return _run

        out = await asyncio.gather(sf.do("a", factory("a")), sf.do("b", factory("b")))
        assert sorted(calls) == ["a", "b"]
        assert sorted(out) == ["a", "b"]

    @pytest.mark.asyncio
    async def test_error_broadcast_to_waiters_and_key_released(self):
        sf = SingleFlight()
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.03)
            raise RuntimeError("boom")

        results = await asyncio.gather(
            *[sf.do("k", factory) for _ in range(3)], return_exceptions=True
        )
        assert calls == 1
        assert all(isinstance(r, RuntimeError) for r in results)
        assert sf.inflight_count == 0

        # 失败后 key 已释放：下一次同 key 重新起跑（不缓存失败）
        async def ok():
            return "ok"

        assert await sf.do("k", ok) == "ok"
        assert calls == 1

    @pytest.mark.asyncio
    async def test_cancelled_waiter_does_not_break_leader(self):
        sf = SingleFlight()
        release = asyncio.Event()

        async def factory():
            await release.wait()
            return "done"

        leader = asyncio.create_task(sf.do("k", factory))
        await asyncio.sleep(0.01)  # 让 leader 先注册 key
        waiter = asyncio.create_task(sf.do("k", factory))
        await asyncio.sleep(0.01)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        assert await leader == "done"
        assert sf.inflight_count == 0

    def test_singleton_and_reset(self):
        assert get_singleflight() is get_singleflight()
        reset_singleflight()
        assert get_singleflight().inflight_count == 0


# ===========================================================================
# 2. SingleFlight 接线（retrieve_scoped_docs_cached）
# ===========================================================================


class TestSingleFlightWiring:
    @pytest.mark.asyncio
    async def test_flag_off_concurrent_miss_runs_twice(self, monkeypatch):
        from agentix.retrieval.usecases import chat_service as cs

        calls = 0

        def counting(*a, **k):
            nonlocal calls
            calls += 1
            return [_doc("法条", 0.9)]

        monkeypatch.setattr(cs, "retrieve_docs_with_retry", counting)
        out = await asyncio.gather(*[_retrieve() for _ in range(2)])
        assert calls == 2  # 基线：不合并
        assert len(out) == 2

    @pytest.mark.asyncio
    async def test_flag_on_concurrent_miss_runs_once(self, monkeypatch):
        from agentix.retrieval.usecases import chat_service as cs

        monkeypatch.setattr(rs, "RETRIEVAL_SINGLEFLIGHT_ENABLED", True)
        calls = 0

        def slow(*a, **k):
            nonlocal calls
            calls += 1
            import time as _t

            _t.sleep(0.05)
            return [_doc("法条", 0.9)]

        monkeypatch.setattr(cs, "retrieve_docs_with_retry", slow)
        out = await asyncio.gather(*[_retrieve() for _ in range(3)])
        assert calls == 1
        assert all(o[0].page_content == "法条" for o in out)

    @pytest.mark.asyncio
    async def test_flag_on_error_propagates_to_all_and_releases(self, monkeypatch):
        from agentix.retrieval.usecases import chat_service as cs

        monkeypatch.setattr(rs, "RETRIEVAL_SINGLEFLIGHT_ENABLED", True)
        calls = 0

        def boom(*a, **k):
            nonlocal calls
            calls += 1
            raise RuntimeError("vector down")

        monkeypatch.setattr(cs, "retrieve_docs_with_retry", boom)
        results = await asyncio.gather(
            *[_retrieve() for _ in range(2)], return_exceptions=True
        )
        assert calls == 1
        assert all(isinstance(r, RuntimeError) for r in results)
        assert get_singleflight().inflight_count == 0


# ===========================================================================
# 3. 级联分类与 runner
# ===========================================================================


class TestClassify:
    def test_statute_by_article_marker(self):
        assert classify_query_kind("第三十八条 保密义务如何约定?") == "statute"
        assert classify_query_kind("第100条说了什么") == "statute"

    def test_multi_hop_by_markers(self):
        assert classify_query_kind("对比竞业限制与保密协议的区别") == "multi_hop"
        assert classify_query_kind("两者的关系是什么") == "multi_hop"

    def test_general_fallback(self):
        assert classify_query_kind("试用期最长多久") == "general"
        assert classify_query_kind("") == "general"
        assert classify_query_kind("   ") == "general"


class TestRunCascade:
    @pytest.mark.asyncio
    async def test_gate_off_stops_at_first_nonempty(self):
        ran = []

        async def l1():
            ran.append("l1")
            return [_doc("a", 0.1)]

        async def l2():
            ran.append("l2")
            return [_doc("b", 0.9)]

        docs, report = await run_cascade(
            [CascadeLevel("l1", l1), CascadeLevel("l2", l2)], min_score=0
        )
        assert ran == ["l1"]
        assert docs[0].page_content == "a"
        assert report["gate"] == "pass" and report["stopped_at"] == "l1"

    @pytest.mark.asyncio
    async def test_empty_escalates(self):
        ran = []

        async def empty():
            ran.append("empty")
            return []

        async def full():
            ran.append("full")
            return [_doc("b", 0.9)]

        docs, report = await run_cascade(
            [CascadeLevel("empty", empty), CascadeLevel("full", full)],
            min_score=0,
        )
        assert ran == ["empty", "full"]
        assert docs[0].page_content == "b"
        assert report["stopped_at"] == "full"

    @pytest.mark.asyncio
    async def test_low_score_escalates_when_gate_on(self):
        ran = []

        async def weak():
            ran.append("weak")
            return [_doc("weak", 0.2)]

        async def strong():
            ran.append("strong")
            return [_doc("strong", 0.9)]

        docs, report = await run_cascade(
            [CascadeLevel("weak", weak), CascadeLevel("strong", strong)],
            min_score=0.5,
        )
        assert ran == ["weak", "strong"]
        assert docs[0].page_content == "strong"
        assert report["gate"] == "pass"

    @pytest.mark.asyncio
    async def test_level_error_fail_open_to_next(self):
        async def bad():
            raise RuntimeError("stage down")

        async def good():
            return [_doc("g", 0.9)]

        docs, report = await run_cascade(
            [CascadeLevel("bad", bad), CascadeLevel("good", good)], min_score=0
        )
        assert docs[0].page_content == "g"
        assert report["attempts"][0] == {"level": "bad", "error": "RuntimeError"}

    @pytest.mark.asyncio
    async def test_all_levels_raise_reraises_last(self):
        async def bad1():
            raise ValueError("e1")

        async def bad2():
            raise RuntimeError("e2")

        with pytest.raises(RuntimeError):
            await run_cascade(
                [CascadeLevel("b1", bad1), CascadeLevel("b2", bad2)], min_score=0
            )

    @pytest.mark.asyncio
    async def test_exhausted_returns_best_fallback(self):
        async def weak():
            return [_doc("weak", 0.2)]

        async def empty():
            return []

        async def bad():
            raise RuntimeError("down")

        docs, report = await run_cascade(
            [
                CascadeLevel("weak", weak),
                CascadeLevel("empty", empty),
                CascadeLevel("bad", bad),
            ],
            min_score=0.5,
        )
        assert docs[0].page_content == "weak"
        assert report["gate"] == "exhausted_fallback"

    @pytest.mark.asyncio
    async def test_all_empty_returns_empty(self):
        async def empty():
            return []

        docs, report = await run_cascade(
            [CascadeLevel("e", empty)], min_score=0
        )
        assert docs == []
        assert report["gate"] == "exhausted_empty"

    @pytest.mark.asyncio
    async def test_unmeasurable_score_treated_as_pass(self):
        async def noscore():
            return [_doc("plain")]  # 无 _retrieval_score ⇒ 无法判定质量 ⇒ 过闸

        docs, report = await run_cascade(
            [CascadeLevel("plain", noscore)], min_score=0.99
        )
        assert docs and report["gate"] == "pass"


class TestNormalizeScore:
    def test_similarity_mode_uses_raw(self):
        docs = [_doc("a", 0.7), _doc("b", 0.3)]
        assert normalize_level_score(docs, higher_is_better=True) == 0.7

    def test_distance_mode_inverts(self):
        docs = [_doc("a", 0.25), _doc("b", 4.0)]
        # 距离越小越好：0.25 → 1/(1+0.25) = 0.8
        assert normalize_level_score(docs, higher_is_better=False) == pytest.approx(
            0.8
        )

    def test_empty_is_negative_inf(self):
        assert normalize_level_score([], higher_is_better=True) == float("-inf")

    def test_unscorable_is_none(self):
        assert normalize_level_score([_doc("plain")], higher_is_better=True) is None


# ===========================================================================
# 4. 自适应级联接线（chat_service）
# ===========================================================================


class TestAdaptiveWiring:
    def _install_hybrid(self, monkeypatch, calls, docs, report=None):
        import agentix.retrieval.usecases.retrieval_hybrid as rh

        async def fake_hybrid(**kw):
            calls.append(kw)
            return list(docs), report or {"stages": [], "docs_count": len(docs)}

        monkeypatch.setattr(rh, "retrieve_hybrid_docs", fake_hybrid)

    @pytest.mark.asyncio
    async def test_gate_off_returns_dense_without_hybrid(self, monkeypatch):
        from agentix.retrieval.usecases import chat_service as cs

        monkeypatch.setattr(rs, "RETRIEVAL_ADAPTIVE_ENABLED", True)
        monkeypatch.setattr(rs, "RETRIEVAL_HYBRID_ENABLED", True)
        dense_calls, hybrid_calls = [], []

        def dense(*a, **k):
            dense_calls.append(k)
            return [_doc("dense", 0.4)]

        monkeypatch.setattr(cs, "retrieve_docs_with_retry", dense)
        self._install_hybrid(monkeypatch, hybrid_calls, [_doc("hyb", 0.9)])

        out = await _retrieve(question="试用期多久")
        assert out[0].page_content == "dense"
        assert len(dense_calls) == 1 and hybrid_calls == []

    @pytest.mark.asyncio
    async def test_general_low_score_escalates_to_hybrid(self, monkeypatch):
        from agentix.retrieval.usecases import chat_service as cs

        monkeypatch.setattr(rs, "RETRIEVAL_ADAPTIVE_ENABLED", True)
        monkeypatch.setattr(rs, "RETRIEVAL_HYBRID_ENABLED", True)
        monkeypatch.setattr(rs, "RETRIEVAL_CASCADE_MIN_SCORE", 0.5)
        monkeypatch.setattr(rs, "INDEX_SCORE_IS_SIMILARITY", True)
        dense_calls, hybrid_calls = [], []

        def dense(*a, **k):
            dense_calls.append(k)
            return [_doc("weak", 0.2)]

        monkeypatch.setattr(cs, "retrieve_docs_with_retry", dense)
        self._install_hybrid(monkeypatch, hybrid_calls, [_doc("strong", 0.9)])

        out = await _retrieve(question="试用期多久")
        assert out[0].page_content == "strong"
        assert len(dense_calls) == 1 and len(hybrid_calls) == 1

    @pytest.mark.asyncio
    async def test_multi_hop_runs_hybrid_first(self, monkeypatch):
        from agentix.retrieval.usecases import chat_service as cs

        monkeypatch.setattr(rs, "RETRIEVAL_ADAPTIVE_ENABLED", True)
        monkeypatch.setattr(rs, "RETRIEVAL_HYBRID_ENABLED", True)
        monkeypatch.setattr(rs, "RETRIEVAL_CASCADE_MIN_SCORE", 0.5)
        monkeypatch.setattr(rs, "INDEX_SCORE_IS_SIMILARITY", True)

        def dense(*a, **k):
            raise AssertionError("multi_hop 首级是融合召回，dense 不应先跑")

        monkeypatch.setattr(cs, "retrieve_docs_with_retry", dense)
        hybrid_calls = []
        self._install_hybrid(monkeypatch, hybrid_calls, [_doc("pair", 0.9)])

        out = await _retrieve(question="对比竞业限制与保密协议的区别")
        assert out[0].page_content == "pair"
        assert len(hybrid_calls) == 1

    @pytest.mark.asyncio
    async def test_statute_orders_dense_before_hybrid(self, monkeypatch):
        from agentix.retrieval.usecases import chat_service as cs

        monkeypatch.setattr(rs, "RETRIEVAL_ADAPTIVE_ENABLED", True)
        monkeypatch.setattr(rs, "RETRIEVAL_HYBRID_ENABLED", True)
        monkeypatch.setattr(rs, "RETRIEVAL_CASCADE_MIN_SCORE", 0.5)
        monkeypatch.setattr(rs, "INDEX_SCORE_IS_SIMILARITY", True)
        order = []

        def dense(*a, **k):
            order.append("dense")
            return [_doc("article", 0.9)]

        monkeypatch.setattr(cs, "retrieve_docs_with_retry", dense)

        async def fake_hybrid(**kw):
            order.append("hybrid")
            return [_doc("x", 0.9)], {"stages": []}

        import agentix.retrieval.usecases.retrieval_hybrid as rh

        monkeypatch.setattr(rh, "retrieve_hybrid_docs", fake_hybrid)

        out = await _retrieve(question="第三十八条说了什么")
        assert out[0].page_content == "article"
        assert order == ["dense"]


# ===========================================================================
# 5. 缓存键升级（租户 + 融合指纹 + |k2）
# ===========================================================================


class TestCacheKeyUpgrade:
    def _key(self, **kw):
        from agentix.retrieval.usecases.chat_service import _retrieval_cache_key

        base = dict(
            kb_id="kb1",
            artifact_id="a1",
            search_k=5,
            question_norm="合同违约",
            scope_token="tok",
        )
        base.update(kw)
        return _retrieval_cache_key(**base)

    def test_contains_tenant_fingerprint_and_k2(self):
        key = self._key(tenant_id="t1")
        assert ":t1:kb1:a1:5:tok:" in key
        assert key.endswith("|k2")
        assert len(key.rsplit("|", 1)[-1]) == 2  # k2 版本标记

    def test_tenant_isolation(self):
        assert self._key(tenant_id="t1") != self._key(tenant_id="t2")
        assert self._key(tenant_id="t1") != self._key(tenant_id="default")

    def test_fingerprint_tracks_fusion_params(self, monkeypatch):
        k1 = self._key()
        assert k1 == self._key()  # 确定性
        monkeypatch.setattr(rs, "RETRIEVAL_FUSION_ALPHA", 0.7)
        assert self._key() != k1

    def test_suffix_order_keeps_hybrid_marker_last(self):
        # p76/p84 契约：|h1 / |r1 追加后仍居末位（endswith 断言不破）
        key = self._key() + "|h1" + "|r1"
        assert key.endswith("|r1")
        assert "|k2" in key

    def test_resolve_tenant_failopen(self):
        from agentix.retrieval.usecases.chat_service import _resolve_cache_tenant

        assert _resolve_cache_tenant(None, "kb1") == "default"
        assert _resolve_cache_tenant(object(), "kb1") == "default"  # 非法 session

    def test_resolve_tenant_reads_kb(self, monkeypatch):
        import agentix.retrieval.infra.repos.kb_repo as kb_repo
        from agentix.retrieval.usecases.chat_service import _resolve_cache_tenant

        monkeypatch.setattr(
            kb_repo,
            "get_kb_by_id",
            lambda session, kb_id: SimpleNamespace(tenant_id="t-9"),
        )
        assert _resolve_cache_tenant(object(), "kb1") == "t-9"

    @pytest.mark.asyncio
    async def test_port_adapter_forwards_tenant(self, monkeypatch):
        from agentix.retrieval.usecases import chat_service as cs
        from agentix.retrieval.usecases.retrieval_port_adapter import (
            RetrievalPortAdapter,
        )

        captured = {}

        async def fake_retrieve(**kw):
            captured.update(kw)
            return []

        monkeypatch.setattr(cs, "retrieve_scoped_docs_cached", fake_retrieve)
        monkeypatch.setattr(
            cs,
            "build_retriever",
            lambda index_path: (None, object()),
        )
        import agentix.retrieval.infra.repos.index_repo as index_repo

        monkeypatch.setattr(
            index_repo,
            "get_active_index_artifact",
            lambda session, kb_id: SimpleNamespace(id="art1", index_path="/x"),
        )

        @contextmanager
        def scope():
            yield object()

        adapter = RetrievalPortAdapter(session_scope_factory=scope)
        result = await adapter.search(query="试用期", kb_id="kb1", tenant_id="t1")
        assert captured.get("tenant_id") == "t1"
        status = result["status"] if isinstance(result, dict) else result.status
        assert status == "empty"


# ===========================================================================
# 6. 精排动态跳过（RERANK_DYNAMIC_SKIP_ENABLED）
# ===========================================================================


class TestRerankDynamicSkip:
    def _reason(self, docs):
        from agentix.retrieval.infra.rag.rerank import dynamic_rerank_skip_reason

        return dynamic_rerank_skip_reason(docs)

    def test_flag_off_never_skips(self):
        assert rs.RERANK_DYNAMIC_SKIP_ENABLED is False
        assert self._reason([_doc("a", 0.99)]) is None

    def test_few_candidates_skips(self, monkeypatch):
        monkeypatch.setattr(rs, "RERANK_DYNAMIC_SKIP_ENABLED", True)
        docs = [_doc(f"d{i}", 0.1) for i in range(4)]
        assert self._reason(docs) == "few_candidates"
        assert self._reason([_doc("x", 0.1)]) == "few_candidates"

    def test_high_score_skips_in_similarity_mode(self, monkeypatch):
        monkeypatch.setattr(rs, "RERANK_DYNAMIC_SKIP_ENABLED", True)
        monkeypatch.setattr(rs, "INDEX_SCORE_IS_SIMILARITY", True)
        high = [_doc(f"d{i}", 0.99) for i in range(6)]
        low = [_doc(f"d{i}", 0.5) for i in range(6)]
        assert self._reason(high) == "high_score"
        assert self._reason(low) is None

    def test_distance_mode_conservative_no_skip(self, monkeypatch):
        monkeypatch.setattr(rs, "RERANK_DYNAMIC_SKIP_ENABLED", True)
        monkeypatch.setattr(rs, "INDEX_SCORE_IS_SIMILARITY", False)
        docs = [_doc(f"d{i}", 0.01) for i in range(6)]
        assert self._reason(docs) is None

    @pytest.mark.asyncio
    async def test_hybrid_stage_records_skip(self, monkeypatch):
        from agentix.retrieval.infra.rag import rerank as rerank_mod

        monkeypatch.setattr(rs, "RETRIEVAL_HYBRID_ENABLED", True)
        monkeypatch.setattr(rs, "RETRIEVAL_SPARSE_ENABLED", False)
        monkeypatch.setattr(rs, "RETRIEVAL_HYBRID_RERANK", True)
        monkeypatch.setattr(rs, "RERANK_DYNAMIC_SKIP_ENABLED", True)
        monkeypatch.setattr(rs, "INDEX_SCORE_IS_SIMILARITY", True)

        def _boom(*a, **k):
            raise AssertionError("动态跳过命中时不得调用 rerank 服务")

        monkeypatch.setattr(rerank_mod, "rerank_documents", _boom)
        monkeypatch.setattr(
            "agentix.retrieval.usecases.chat_service.retrieve_docs_with_retry",
            lambda *a, **k: [_doc(f"d{i}", 0.9) for i in range(2)],
        )

        from agentix.retrieval.usecases import chat_service as cs

        docs = await cs.retrieve_scoped_docs_cached(
            kb_id="kb1",
            question="试用期",
            search_k=5,
            metadata_filter=None,
            vectorstore=object(),
            cache_redis=None,
            artifact_id="a1",
            scope_token="",
            session=None,
        )
        from agentix.retrieval.usecases.retrieval_hybrid import pop_stage_report

        report = pop_stage_report()
        rerank_stage = report["stages"][4]
        assert rerank_stage["applied"] is False
        assert rerank_stage["skipped"] == "few_candidates"
        assert len(docs) == 2

    @pytest.mark.asyncio
    async def test_hybrid_stage_shape_unchanged_when_flag_off(self, monkeypatch):
        from agentix.retrieval.usecases import chat_service as cs
        from agentix.retrieval.usecases.retrieval_hybrid import pop_stage_report

        monkeypatch.setattr(rs, "RETRIEVAL_HYBRID_ENABLED", True)
        monkeypatch.setattr(rs, "RETRIEVAL_SPARSE_ENABLED", False)
        monkeypatch.setattr(rs, "RETRIEVAL_HYBRID_RERANK", False)
        monkeypatch.setattr(
            "agentix.retrieval.usecases.chat_service.retrieve_docs_with_retry",
            lambda *a, **k: [_doc("a", 0.9), _doc("b", 0.8)],
        )

        await cs.retrieve_scoped_docs_cached(
            kb_id="kb1",
            question="试用期",
            search_k=5,
            metadata_filter=None,
            vectorstore=object(),
            cache_redis=None,
            artifact_id="a1",
            scope_token="",
            session=None,
        )
        report = pop_stage_report()
        rerank_stage = report["stages"][4]
        assert rerank_stage["applied"] is False
        assert "skipped" not in rerank_stage  # flag-off 形状与基线一致

    def test_tool_sites_are_wired(self):
        tools = Path(
            "agentix/retrieval/usecases/chat_agent_tools.py"
        ).read_text(encoding="utf-8")
        port = Path(
            "agentix/retrieval/usecases/retrieval_port_adapter.py"
        ).read_text(encoding="utf-8")
        assert "dynamic_rerank_skip_reason" in tools
        assert "dynamic_rerank_skip_reason" in port
        assert "tenant_id=tenant_id" in port  # W2 端口显式租户透传
