"""F1a：RAG P1 真 Hybrid（R-P1-1..5）契约守卫（p76）。

钉死 §5 的五项 P1：

1. **flag-off 字节等价**：``RETRIEVAL_HYBRID_ENABLED=false``（默认）时
   sparse/融合/精排零调用，走既有 dense 单通道链；
2. R-P1-1 sparse：进程内 BM25——分词/检索/scope 过滤同 FAISS 语义/索引缓存/fail-open；
3. R-P1-2 融合：rrf/zscore 纯函数已知值 + 双通道按 chunk_id 去重融合 + Channel B
   flag-off 仍 postgres-first、flag-on 两路都保留；
4. R-P1-3：hybrid 链内精排默认开（候选 ≤ RETRIEVAL_RERANK_CANDIDATES）、
   关闭可配、精排失败 fail-open 保序；
5. R-P1-4：分通道阈值（dense 距离阈值 / sparse 独立 min_score）、融合后只按
   TopN 截断不复用距离阈值、fetch_k 无 ``index.ntotal`` 时走配置值；
6. R-P1-5：四阶段报告（rewrite/dense/sparse/rrf/rerank + 耗时/命中/TopK/熔断位）
   经 ContextVar 交 SSE 追加帧（flag-off 三段帧不变）；
7. 顺手修复的 tuple 断点（三个真实后端 ``similarity_search`` 返回
   ``(VectorDoc, score)``，此前整包当 doc 直接 AttributeError）。
"""

from __future__ import annotations

import pytest
from langchain_core.documents import Document as LCD

# ---------------------------------------------------------------- 0. 配置面


class TestHybridConfig:
    def test_retrieval_settings_keys_and_defaults(self):
        from agentix.retrieval.settings import settings as rs

        assert rs.RETRIEVAL_HYBRID_ENABLED is False
        assert rs.RETRIEVAL_SPARSE_ENABLED is False
        assert rs.RETRIEVAL_SPARSE_MIN_SCORE == 0.0
        assert rs.RETRIEVAL_SPARSE_CACHE_TTL == 600
        assert rs.RETRIEVAL_FUSION_METHOD == "rrf"
        assert rs.RETRIEVAL_RRF_K == 60
        assert rs.RETRIEVAL_FUSION_ALPHA == 0.5
        assert rs.RETRIEVAL_FUSION_TOP_N == 200
        assert rs.RETRIEVAL_HYBRID_RERANK is True  # R-P1-3：链内默认开（随总闸）
        assert rs.RETRIEVAL_RERANK_CANDIDATES == 20

    def test_unified_kb_fusion_keys(self):
        from agentix.config.settings import settings

        assert settings.KB_LOCAL_FUSION_ENABLED is False
        assert settings.KB_LOCAL_FUSION_METHOD == "rrf"

    def test_env_example_registers_knobs_as_comment_lines(self):
        """旋钮可发现且为注释示例形态（防 .env 自检"缺项"噪声）。"""
        from pathlib import Path

        from agentix.config.selfcheck import parse_env_keys

        example = Path(__file__).resolve().parents[1] / ".env.example"
        text = example.read_text(encoding="utf-8")
        live = parse_env_keys(example)
        names = (
            "RETRIEVAL_HYBRID_ENABLED",
            "RETRIEVAL_SPARSE_ENABLED",
            "RETRIEVAL_SPARSE_MIN_SCORE",
            "RETRIEVAL_SPARSE_CACHE_TTL",
            "RETRIEVAL_FUSION_METHOD",
            "RETRIEVAL_RRF_K",
            "RETRIEVAL_FUSION_ALPHA",
            "RETRIEVAL_FUSION_TOP_N",
            "RETRIEVAL_HYBRID_RERANK",
            "RETRIEVAL_RERANK_CANDIDATES",
            "KB_LOCAL_FUSION_ENABLED",
            "KB_LOCAL_FUSION_METHOD",
        )
        for name in names:
            assert name in text, f"{name} 未在 .env.example 登记（运维无从发现）"
            assert name not in live, f"{name} 写成了生效行（应保持注释示例形态）"


# ---------------------------------------------------------------- 1. 融合纯函数（R-P1-2）


class TestRankFusion:
    def test_rrf_known_values_and_cross_channel_boost(self):
        from agentix.domain.policies.rank_fusion import rrf_fuse

        out = rrf_fuse([["a", "b", "c"], ["b", "d", "a"]], k=60)
        ids = [k for k, _ in out]
        # 双通道都出现的 b 必须压过单通道 rank1 的 a/c
        assert ids[0] == "b"
        assert set(ids) == {"a", "b", "c", "d"}
        scores = dict(out)
        assert scores["b"] > scores["a"] > scores["d"] > scores["c"]

    def test_rrf_k_and_top_n_and_dedupe(self):
        from agentix.domain.policies.rank_fusion import rrf_fuse

        assert [k for k, _ in rrf_fuse([["x", "y", "z"]], top_n=2)] == ["x", "y"]
        # 同通道重复 id 只计一次（首次出现）
        out = rrf_fuse([["x", "x", "y"]])
        assert [k for k, _ in out] == ["x", "y"]
        assert rrf_fuse([]) == []
        assert rrf_fuse([[]]) == []
        # k 越大分数越小
        s_small = rrf_fuse([["a", "b"]], k=1)[0][1]
        s_large = rrf_fuse([["a", "b"]], k=600)[0][1]
        assert s_small > s_large

    def test_zscore_alpha_weights_and_degenerate_std(self):
        from agentix.domain.policies.rank_fusion import zscore_fuse

        # ch1 认 a、ch2 认 b：alpha=1 → 完全听 ch1；alpha=0 → 完全听 ch2
        ch1 = [("a", 1.0), ("b", 0.0)]
        ch2 = [("b", 9.0), ("a", 1.0)]
        assert zscore_fuse([ch1, ch2], alpha=1.0)[0][0] == "a"
        assert zscore_fuse([ch1, ch2], alpha=0.0)[0][0] == "b"
        # 同分通道（std=0）记 0，不制造假区分度
        same = [("a", 5.0), ("b", 5.0)]
        out = zscore_fuse([same, [("c", 1.0)]], alpha=0.5)
        assert {k for k, _ in out} == {"a", "b", "c"}
        assert zscore_fuse([]) == []

    def test_meta_matches_filter_semantics(self):
        from agentix.retrieval.domain.services.chat_scope import (
            build_faiss_filter,
            meta_matches_filter,
        )

        assert meta_matches_filter({"document_id": "d1"}, None) is True
        assert meta_matches_filter({"document_id": "d1"}, {"document_id": "d1"}) is True
        assert meta_matches_filter({"document_id": "d2"}, {"document_id": "d1"}) is False
        assert meta_matches_filter({"document_id": "d1"}, {"document_id": ["d1", "d2"]}) is True
        assert meta_matches_filter(None, {"document_id": "d1"}) is False
        # 与 build_faiss_filter 产出的三种形态同语义
        assert meta_matches_filter({"document_id": "d1"}, build_faiss_filter(["d1"])) is True
        assert meta_matches_filter({"document_id": "d1"}, build_faiss_filter(["d1", "d2"])) is True
        assert meta_matches_filter({"document_id": "d9"}, build_faiss_filter(["d1", "d2"])) is False
        assert meta_matches_filter({"document_id": "d1"}, build_faiss_filter([])) is True
        # callable 异常 fail-closed
        assert meta_matches_filter({}, lambda m: (_ for _ in ()).throw(ValueError())) is False


# ---------------------------------------------------------------- 2. sparse 路（R-P1-1）


class TestSparseBM25:
    def test_tokenize_ascii_and_cjk(self):
        from agentix.retrieval.infra.rag.sparse import tokenize

        toks = tokenize("民法典 Article 123 合同")
        assert "article" in toks and "123" in toks
        assert "民法" in toks and "法典" in toks and "民" in toks
        assert tokenize("") == []

    def test_article_number_tokens_normalize_chinese_and_arabic_forms(self):
        from agentix.retrieval.infra.rag.sparse import tokenize

        assert "article:148" in tokenize("《民法典》第148条")
        assert "article:148" in tokenize("《民法典》第一百四十八条")
        assert "article:105" in tokenize("第一百零五条")
        assert "article:10" in tokenize("第十条")
        assert "article:2023" in tokenize("第二〇二三条")
        assert "article:1234567890123" not in tokenize("第1234567890123条")

    def test_bm25_matches_chinese_and_arabic_article_number_forms(self):
        from agentix.retrieval.infra.rag.sparse import BM25Index

        index = BM25Index(
            [
                ("arabic", "《民法典》第148条 合同撤销", {}),
                ("chinese", "《民法典》第一百四十八条 合同撤销", {}),
                ("other", "《民法典》第一百四十九条 合同效力", {}),
            ]
        )
        arabic_hits = index.search("民法典 第一百四十八条", top_n=3)
        chinese_hits = index.search("民法典 第148条", top_n=3)

        assert {doc_id for doc_id, _ in arabic_hits[:2]} == {"arabic", "chinese"}
        assert {doc_id for doc_id, _ in chinese_hits[:2]} == {"arabic", "chinese"}
        assert "other" not in {doc_id for doc_id, _ in chinese_hits[:2]}

    def test_bm25_index_ranks_matching_doc_first(self):
        from agentix.retrieval.infra.rag.sparse import BM25Index

        index = BM25Index(
            [
                ("c1", "民法典 合同编 违约责任", {"chunk_id": "c1", "document_id": "d1"}),
                ("c2", "刑法总则 犯罪构成", {"chunk_id": "c2", "document_id": "d2"}),
                ("c3", "民法典 物权编 所有权", {"chunk_id": "c3", "document_id": "d1"}),
            ]
        )
        hits = index.search("民法典 违约", top_n=5)
        assert hits[0][0] == "c1"
        assert index.search("不存在词汇xyz", top_n=5) == []
        assert index.search("民法典", top_n=0) == []
        assert index.meta_for("c1")["document_id"] == "d1"
        assert index.meta_for("nope") == {}
        assert "刑法" in index.content_for("c2")
        assert len(index) == 3

    def test_inverted_bm25_matches_exhaustive_scoring_and_stable_ties(self):
        from collections import Counter
        import math

        from agentix.retrieval.infra.rag.sparse import BM25Index, tokenize

        records = [
            ("c1", "民法典 合同 违约责任 合同", {}),
            ("c2", "民法典 合同 违约责任 合同", {}),
            ("c3", "民法典 物权 所有权", {}),
            ("c4", "刑法 犯罪 构成", {}),
        ]
        query_terms = set(tokenize("民法典 合同"))
        term_frequencies = [Counter(tokenize(text)) for _, text, _ in records]
        doc_lengths = [sum(tf.values()) for tf in term_frequencies]
        avgdl = sum(doc_lengths) / len(doc_lengths)
        idf = {
            term: math.log(
                1.0
                + (len(records) - sum(term in tf for tf in term_frequencies) + 0.5)
                / (sum(term in tf for tf in term_frequencies) + 0.5)
            )
            for term in query_terms
        }
        expected = []
        for idx, tf in enumerate(term_frequencies):
            norm = 1.5 * (1.0 - 0.75 + 0.75 * doc_lengths[idx] / avgdl)
            score = sum(
                idf[term] * (freq * 2.5) / (freq + norm)
                for term in query_terms
                if (freq := tf.get(term))
            )
            if score > 0:
                expected.append((records[idx][0], score))
        expected.sort(key=lambda row: -row[1])

        actual = BM25Index(records).search("民法典 合同", top_n=10)

        assert [doc_id for doc_id, _ in actual] == [doc_id for doc_id, _ in expected]
        assert [score for _, score in actual] == pytest.approx([score for _, score in expected])

    @pytest.fixture
    def _chunks(self):
        class Chunk:
            def __init__(self, i, doc, content):
                self.id = i
                self.document_id = doc
                self.content = content
                self.page_no = 1

        return [
            Chunk("c1", "d1", "民法典 合同编 违约责任"),
            Chunk("c2", "d2", "刑法总则 犯罪构成要件"),
            Chunk("c3", "d1", "民法典 物权编"),
        ]

    def test_sparse_search_scope_cache_and_failopen(self, monkeypatch, _chunks):
        import agentix.retrieval.infra.rag.sparse as sp
        from agentix.retrieval.infra.repos import document_repo

        calls = {"n": 0}

        def counting(session, kb_id):
            calls["n"] += 1
            return list(_chunks)

        monkeypatch.setattr(document_repo, "get_chunks_by_kb", counting)
        sp.clear_cache()
        try:
            out = sp.sparse_search(session=object(), kb_id="kb1", query="民法典", top_n=5)
            assert out and out[0][0].page_content.startswith("民法典")
            # TTL 缓存：第二次不重载语料
            sp.sparse_search(session=object(), kb_id="kb1", query="民法典", top_n=5)
            assert calls["n"] == 1
            # scope：dict 过滤与 FAISS 同语义（d1 两条被裁掉）
            scoped = sp.sparse_search(
                session=object(),
                kb_id="kb1",
                query="民法典",
                top_n=5,
                metadata_filter={"document_id": "d2"},
            )
            assert all(d.metadata["document_id"] == "d2" for d, _ in scoped)
            # callable 过滤（folder scope 形态）
            foldered = sp.sparse_search(
                session=object(),
                kb_id="kb1",
                query="民法典",
                top_n=5,
                metadata_filter=lambda meta: meta.get("document_id") in {"d1"},
            )
            assert foldered and all(d.metadata["document_id"] == "d1" for d, _ in foldered)
            # 空查询/零 top_n 快速返回
            assert sp.sparse_search(session=object(), kb_id="kb1", query="  ", top_n=5) == []
            assert sp.sparse_search(session=object(), kb_id="kb1", query="q", top_n=0) == []
        finally:
            sp.clear_cache()

        # fail-open：语料加载异常 → []（绝不抛出）
        def boom(session, kb_id):
            raise RuntimeError("db down")

        monkeypatch.setattr(document_repo, "get_chunks_by_kb", boom)
        sp.clear_cache()
        assert sp.sparse_search(session=object(), kb_id="kb1", query="民法典", top_n=5) == []
        sp.clear_cache()

    def test_sparse_cache_isolated_by_active_artifact(self, monkeypatch):
        import agentix.retrieval.infra.rag.sparse as sp
        from agentix.retrieval.infra.repos import document_repo

        class Chunk:
            def __init__(self, chunk_id, content):
                self.id = chunk_id
                self.document_id = "doc1"
                self.content = content
                self.page_no = 1

        calls = []

        def get_artifact_chunks(session, kb_id, artifact_id):
            calls.append(artifact_id)
            return [Chunk(artifact_id, f"合同条款 {artifact_id}")]

        monkeypatch.setattr(document_repo, "get_chunks_for_index_artifact", get_artifact_chunks)
        sp.clear_cache()
        try:
            old = sp.sparse_search(
                session=object(),
                kb_id="kb1",
                query="合同条款",
                top_n=5,
                artifact_id="artifact-v1",
            )
            cached = sp.sparse_search(
                session=object(),
                kb_id="kb1",
                query="合同条款",
                top_n=5,
                artifact_id="artifact-v1",
            )
            fresh = sp.sparse_search(
                session=object(),
                kb_id="kb1",
                query="合同条款",
                top_n=5,
                artifact_id="artifact-v2",
            )

            assert old[0][0].metadata["chunk_id"] == "artifact-v1"
            assert cached[0][0].metadata["chunk_id"] == "artifact-v1"
            assert fresh[0][0].metadata["chunk_id"] == "artifact-v2"
            assert calls == ["artifact-v1", "artifact-v2"]
        finally:
            sp.clear_cache()

    def test_sparse_corpus_uses_only_ready_chunks_in_active_manifest(self):
        from sqlalchemy.pool import StaticPool
        from sqlmodel import Session, SQLModel, create_engine

        from agentix.retrieval.domain.models import (
            Chunk,
            Document,
            IndexArtifact,
            IndexArtifactManifest,
        )
        from agentix.retrieval.infra.repos.document_repo import (
            get_chunks_for_index_artifact,
        )

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.add_all(
                [
                    IndexArtifact(
                        id="active",
                        kb_id="kb1",
                        version=2,
                        index_path="active",
                        index_job_id="job2",
                        is_active=True,
                    ),
                    IndexArtifact(
                        id="inactive",
                        kb_id="kb1",
                        version=1,
                        index_path="inactive",
                        index_job_id="job1",
                        is_active=False,
                    ),
                    Document(
                        id="ready",
                        kb_id="kb1",
                        title="ready",
                        status="ready",
                        indexed_artifact_id="active",
                    ),
                    Document(
                        id="indexing",
                        kb_id="kb1",
                        title="indexing",
                        status="indexing",
                        indexed_artifact_id="active",
                    ),
                    Document(
                        id="old",
                        kb_id="kb1",
                        title="old",
                        status="ready",
                        indexed_artifact_id="inactive",
                    ),
                    Chunk(id="c-ready", document_id="ready", content="current"),
                    Chunk(id="c-indexing", document_id="indexing", content="pending"),
                    Chunk(id="c-old", document_id="old", content="stale"),
                    IndexArtifactManifest(
                        id="m-ready",
                        artifact_id="active",
                        document_id="ready",
                        content_hash="h1",
                        chunk_count=1,
                    ),
                    IndexArtifactManifest(
                        id="m-indexing",
                        artifact_id="active",
                        document_id="indexing",
                        content_hash="h2",
                        chunk_count=1,
                    ),
                    IndexArtifactManifest(
                        id="m-old",
                        artifact_id="inactive",
                        document_id="old",
                        content_hash="h0",
                        chunk_count=1,
                    ),
                ]
            )
            session.commit()

            chunks = get_chunks_for_index_artifact(session, "kb1", "active")

        assert [chunk.id for chunk in chunks] == ["c-ready"]

    def test_sparse_min_score_cutoff(self, monkeypatch, _chunks):
        import agentix.retrieval.infra.rag.sparse as sp
        from agentix.retrieval.infra.repos import document_repo

        monkeypatch.setattr(document_repo, "get_chunks_by_kb", lambda s, k: list(_chunks))
        sp.clear_cache()
        try:
            normal = sp.sparse_search(session=object(), kb_id="kb1", query="民法典", top_n=5)
            assert normal
            top_score = normal[0][1]
            cutoff = sp.sparse_search(
                session=object(),
                kb_id="kb1",
                query="民法典",
                top_n=5,
                min_score=top_score,  # score <= min_score 全裁
            )
            assert cutoff == []
        finally:
            sp.clear_cache()


# ---------------------------------------------------------------- 3. tuple 断点回归（R-P1-4 前提）


class TestBackendTupleUnpack:
    def test_tuple_backend_unpacks_document_and_live_score(self):
        from agentix.retrieval.settings import settings as rs
        from agentix.retrieval.usecases.chat_service import _similarity_search_with_fetch

        class VectorDocLike:  # faiss/milvus/qdrant 共同形状：text/metadata/id，无 page_content
            def __init__(self, text, meta):
                self.text = text
                self.metadata = meta
                self.id = meta.get("chunk_id")

        class Store:
            def similarity_search(self, q, k=5, filter=None, fetch_k=None):
                return [
                    (VectorDocLike("命中", {"chunk_id": "a"}), 0.9),
                    (VectorDocLike("超阈", {"chunk_id": "b"}), 99.0),
                ]

        docs = _similarity_search_with_fetch(
            Store(), "q", search_k=5, metadata_filter=None, fetch_k=10
        )
        # 真实分数流入后距离阈值（默认 1.2）实际生效：99.0 被裁掉
        assert [d.page_content for d in docs] == ["命中"]
        assert docs[0].metadata["_retrieval_score"] == 0.9
        assert rs.RETRIEVAL_MAX_DISTANCE == 1.2  # 前提钉死

    def test_plain_documents_passthrough_score_zero(self):
        """非 tuple 元素（langchain Document）保持旧行为：score 0.0 透传。"""
        from agentix.retrieval.usecases.chat_service import _similarity_search_with_fetch

        class Store:
            def similarity_search(self, q, k=5, filter=None, fetch_k=None):
                return [LCD(page_content="裸文档", metadata={"chunk_id": "x"})]

        docs = _similarity_search_with_fetch(
            Store(), "q", search_k=5, metadata_filter=None, fetch_k=10
        )
        assert len(docs) == 1
        assert docs[0].page_content == "裸文档"
        assert docs[0].metadata["_retrieval_score"] == 0.0

    def test_fetch_k_config_driven_without_index_attr(self):
        """R-P1-4：无 index.ntotal（milvus/qdrant 包装器）→ 恒走配置值，不崩不依赖。"""
        from agentix.retrieval.usecases.chat_service import _resolve_retrieval_fetch_k

        class VS:  # 无 .index 属性
            pass

        base = _resolve_retrieval_fetch_k(VS(), 5, None, post_filtered=False)
        bounded = _resolve_retrieval_fetch_k(VS(), 5, {"document_id": "d"}, post_filtered=True)
        assert base == max(5 * 4, 20)
        assert bounded >= base


# ---------------------------------------------------------------- 4. 总闸：flag-off / flag-on 主链


def _make_docs(pairs):
    return [
        LCD(page_content=text, metadata={"chunk_id": cid, "_retrieval_score": score})
        for cid, text, score in pairs
    ]


class TestHybridChain:
    @pytest.mark.asyncio
    async def test_flag_off_dense_only_sparse_never_called(self, monkeypatch):
        import agentix.retrieval.infra.rag.sparse as sp
        import agentix.retrieval.usecases.chat_service as cs
        from agentix.retrieval.settings import settings as rs

        calls = {"dense": 0, "sparse": 0}
        monkeypatch.setattr(rs, "RETRIEVAL_HYBRID_ENABLED", False)
        monkeypatch.setattr(rs, "RETRIEVAL_SPARSE_ENABLED", True)  # 子闸无效：总闸关

        def fake_dense(vectorstore, question, *, search_k, metadata_filter, **kw):
            calls["dense"] += 1
            return _make_docs([("a", "甲", 0.9)])

        def exploding_sparse(**kw):
            calls["sparse"] += 1
            raise AssertionError("flag-off 不得走 sparse")

        monkeypatch.setattr(cs, "retrieve_docs_with_retry", fake_dense)
        monkeypatch.setattr(sp, "sparse_search", exploding_sparse)

        docs = await cs.retrieve_scoped_docs_cached(
            kb_id="kb1",
            question="问",
            search_k=3,
            metadata_filter=None,
            vectorstore=object(),
            cache_redis=None,
            artifact_id="art1",
            scope_token="tok",
            session=object(),
        )
        assert [d.page_content for d in docs] == ["甲"]
        assert calls == {"dense": 1, "sparse": 0}

    @pytest.mark.asyncio
    async def test_flag_on_fuses_dense_and_sparse_by_chunk_id(self, monkeypatch):
        import agentix.retrieval.infra.rag.sparse as sp
        import agentix.retrieval.usecases.chat_service as cs
        from agentix.retrieval.settings import settings as rs
        from agentix.retrieval.usecases.retrieval_hybrid import (
            retrieve_hybrid_docs,
        )

        monkeypatch.setattr(rs, "RETRIEVAL_HYBRID_ENABLED", True)
        monkeypatch.setattr(rs, "RETRIEVAL_SPARSE_ENABLED", True)
        monkeypatch.setattr(rs, "RETRIEVAL_HYBRID_RERANK", False)

        def fake_dense(vectorstore, question, *, search_k, metadata_filter, **kw):
            return _make_docs([("a", "dense甲", 0.9), ("b", "dense乙", 0.8)])

        def fake_sparse(**kw):
            return [
                (
                    LCD(page_content="共享条", metadata={"chunk_id": "b", "document_id": "d1"}),
                    3.2,
                ),
                (
                    LCD(page_content="sparse独有", metadata={"chunk_id": "z", "document_id": "d1"}),
                    1.1,
                ),
            ]

        monkeypatch.setattr(cs, "retrieve_docs_with_retry", fake_dense)
        monkeypatch.setattr(sp, "sparse_search", fake_sparse)

        docs, report = await retrieve_hybrid_docs(
            kb_id="kb1",
            question="问?",
            question_norm="问?",
            search_k=3,
            metadata_filter=None,
            vectorstore=object(),
            artifact_id="art1",
            rewrite_ms=0.1,
            session=object(),
        )
        ids = [d.metadata["chunk_id"] for d in docs]
        # 双通道共享的 b 被 RRF 抬到第一；单通道命中随后
        assert ids[0] == "b"
        assert set(ids) == {"a", "b", "z"}
        # 融合分写回（不再是任一通道原分）
        b_doc = next(d for d in docs if d.metadata["chunk_id"] == "b")
        # b 是 dense 第 2 名 + sparse 第 1 名：1/(k+2) + 1/(k+1)
        assert b_doc.metadata["_retrieval_score"] == pytest.approx(
            1.0 / (rs.RETRIEVAL_RRF_K + 2) + 1.0 / (rs.RETRIEVAL_RRF_K + 1)
        )

    @pytest.mark.asyncio
    async def test_dense_and_sparse_retrieval_run_concurrently(self, monkeypatch):
        import threading

        import agentix.retrieval.infra.rag.sparse as sp
        import agentix.retrieval.usecases.chat_service as cs
        from agentix.retrieval.settings import settings as rs
        from agentix.retrieval.usecases.retrieval_hybrid import retrieve_hybrid_docs

        monkeypatch.setattr(rs, "RETRIEVAL_SPARSE_ENABLED", True)
        monkeypatch.setattr(rs, "RETRIEVAL_HYBRID_RERANK", False)
        both_started = threading.Barrier(2)

        def fake_dense(*args, **kwargs):
            both_started.wait(timeout=5)
            return _make_docs([("dense", "dense", 0.9)])

        def fake_sparse(**kwargs):
            both_started.wait(timeout=5)
            return []

        monkeypatch.setattr(cs, "retrieve_docs_with_retry", fake_dense)
        monkeypatch.setattr(sp, "sparse_search", fake_sparse)

        docs, report = await retrieve_hybrid_docs(
            kb_id="kb1",
            question="问",
            question_norm="问",
            search_k=2,
            metadata_filter=None,
            vectorstore=object(),
            artifact_id="art1",
            rewrite_ms=0.0,
            session=object(),
        )

        assert [doc.metadata["chunk_id"] for doc in docs] == ["dense"]
        assert [stage["stage"] for stage in report["stages"][:3]] == [
            "rewrite",
            "dense",
            "sparse",
        ]

    @pytest.mark.asyncio
    async def test_stage_report_shape(self, monkeypatch):
        import agentix.retrieval.infra.rag.sparse as sp
        import agentix.retrieval.usecases.chat_service as cs
        from agentix.retrieval.settings import settings as rs
        from agentix.retrieval.usecases.retrieval_hybrid import retrieve_hybrid_docs

        monkeypatch.setattr(rs, "RETRIEVAL_SPARSE_ENABLED", True)
        monkeypatch.setattr(rs, "RETRIEVAL_HYBRID_RERANK", False)
        monkeypatch.setattr(
            cs, "retrieve_docs_with_retry", lambda *a, **kw: _make_docs([("a", "甲", 0.5)])
        )
        monkeypatch.setattr(
            sp,
            "sparse_search",
            lambda **kw: [(LCD(page_content="乙", metadata={"chunk_id": "z"}), 2.0)],
        )

        _, report = await retrieve_hybrid_docs(
            kb_id="kb1",
            question="问",
            question_norm="问",
            search_k=2,
            metadata_filter=None,
            vectorstore=object(),
            artifact_id="art1",
            rewrite_ms=0.2,
            session=object(),
        )
        stages = report["stages"]
        assert [s["stage"] for s in stages] == [
            "rewrite",
            "dense",
            "sparse",
            "rrf",
            "rerank",
        ]
        for entry in stages:
            assert isinstance(entry["ms"], float)
        assert stages[0]["ms"] == pytest.approx(0.2)
        dense_stage = stages[1]
        assert dense_stage["hits"] == 1 and dense_stage["topk"]
        rrf_stage = stages[3]
        assert rrf_stage["method"] == "rrf" and rrf_stage["hits"] == 2
        rerank_stage = stages[4]
        assert rerank_stage["applied"] is False
        assert rerank_stage["breaker"]  # 熔断位/禁用态作为归因供数
        assert report["docs_count"] == 2
        assert report["fusion_method"] == "rrf"

    @pytest.mark.asyncio
    async def test_sparse_skipped_without_session(self, monkeypatch):
        from agentix.retrieval.settings import settings as rs
        from agentix.retrieval.usecases import chat_service as cs
        from agentix.retrieval.usecases.retrieval_hybrid import retrieve_hybrid_docs

        monkeypatch.setattr(rs, "RETRIEVAL_SPARSE_ENABLED", True)
        monkeypatch.setattr(rs, "RETRIEVAL_HYBRID_RERANK", False)
        monkeypatch.setattr(
            cs, "retrieve_docs_with_retry", lambda *a, **kw: _make_docs([("a", "甲", 0.5)])
        )

        docs, report = await retrieve_hybrid_docs(
            kb_id="kb1",
            question="问",
            question_norm="问",
            search_k=2,
            metadata_filter=None,
            vectorstore=object(),
            artifact_id="art1",
            rewrite_ms=0.0,
            session=None,  # 无 session → sparse fail-open 跳过，dense 照常
        )
        sparse_stage = report["stages"][2]
        assert sparse_stage["skipped"] == "no_session"
        assert [d.page_content for d in docs] == ["甲"]
        assert report["sparse"] == "no_session"

    @pytest.mark.asyncio
    async def test_rerank_default_on_candidates_cap_and_failopen(self, monkeypatch):
        import agentix.retrieval.infra.rag.rerank as rerank_mod
        import agentix.retrieval.usecases.chat_service as cs
        from agentix.retrieval.settings import settings as rs
        from agentix.retrieval.usecases.retrieval_hybrid import retrieve_hybrid_docs

        monkeypatch.setattr(rs, "RETRIEVAL_SPARSE_ENABLED", False)
        monkeypatch.setattr(rs, "RETRIEVAL_HYBRID_RERANK", True)
        monkeypatch.setattr(rs, "RETRIEVAL_RERANK_CANDIDATES", 3)
        monkeypatch.setattr(rs, "RETRIEVAL_FUSION_TOP_N", 100)

        many = _make_docs([(f"c{i}", f"文{i}", 1.0 - i * 0.01) for i in range(6)])
        monkeypatch.setattr(cs, "retrieve_docs_with_retry", lambda *a, **kw: list(many))

        seen = {}

        async def fake_rerank(query, docs, model=None):
            seen["n"] = len(docs)
            seen["query"] = query
            return list(reversed(docs)), True

        monkeypatch.setattr(rerank_mod, "rerank_documents_with_status", fake_rerank)

        docs, report = await retrieve_hybrid_docs(
            kb_id="kb1",
            question="原始问句",
            question_norm="原始问句",
            search_k=2,
            metadata_filter=None,
            vectorstore=object(),
            artifact_id="art1",
            rewrite_ms=0.0,
            session=None,
        )
        assert seen["n"] == 3  # 候选上限（Top20 默认 → 本测试 3）
        rerank_stage = report["stages"][4]
        assert rerank_stage["applied"] is True and rerank_stage["hits"] == 3
        assert [d.metadata["chunk_id"] for d in docs] == ["c2", "c1"]  # 逆序后截断 TopK

        # 精排抛异常 → fail-open 保序（候选原序、截断 TopK）
        async def broken_rerank(query, docs, model=None):
            raise RuntimeError("rerank 服务挂了")

        monkeypatch.setattr(rerank_mod, "rerank_documents_with_status", broken_rerank)
        docs2, report2 = await retrieve_hybrid_docs(
            kb_id="kb1",
            question="原始问句",
            question_norm="原始问句",
            search_k=2,
            metadata_filter=None,
            vectorstore=object(),
            artifact_id="art1",
            rewrite_ms=0.0,
            session=None,
        )
        assert [d.metadata["chunk_id"] for d in docs2] == ["c0", "c1"]
        assert report2["stages"][4]["applied"] is False

    @pytest.mark.asyncio
    async def test_zscore_fusion_method_used(self, monkeypatch):
        import agentix.retrieval.infra.rag.sparse as sp
        import agentix.retrieval.usecases.chat_service as cs
        from agentix.retrieval.settings import settings as rs
        from agentix.retrieval.usecases.retrieval_hybrid import retrieve_hybrid_docs

        monkeypatch.setattr(rs, "RETRIEVAL_SPARSE_ENABLED", True)
        monkeypatch.setattr(rs, "RETRIEVAL_FUSION_METHOD", "zscore")
        monkeypatch.setattr(rs, "RETRIEVAL_HYBRID_RERANK", False)
        monkeypatch.setattr(
            cs,
            "retrieve_docs_with_retry",
            lambda *a, **kw: _make_docs([("a", "甲", 0.9), ("b", "乙", 0.8)]),
        )
        monkeypatch.setattr(
            sp,
            "sparse_search",
            lambda **kw: [
                (LCD(page_content="乙", metadata={"chunk_id": "b"}), 5.0),
                (LCD(page_content="丙", metadata={"chunk_id": "z"}), 4.0),
            ],
        )

        docs, report = await retrieve_hybrid_docs(
            kb_id="kb1",
            question="问",
            question_norm="问",
            search_k=3,
            metadata_filter=None,
            vectorstore=object(),
            artifact_id="art1",
            rewrite_ms=0.0,
            session=object(),
        )
        assert report["fusion_method"] == "zscore"
        assert report["stages"][3]["method"] == "zscore"
        assert {d.metadata["chunk_id"] for d in docs} == {"a", "b", "z"}
        assert len(docs) == 3


# ---------------------------------------------------------------- 5. 接线：session / 缓存标记 / SSE 帧


class TestHybridWiring:
    @pytest.mark.asyncio
    async def test_cache_key_marker_separates_hybrid_pool(self, monkeypatch):
        import agentix.retrieval.usecases.chat_service as cs
        from agentix.retrieval.settings import settings as rs

        class FakeRedis:
            def __init__(self):
                self.keys = []
                self.payload = None

            def get(self, key):
                self.keys.append(key)
                return None

            def set(self, key, value, ex=None, nx=False):
                self.payload = (key, value, ex, nx)
                return True

        monkeypatch.setattr(rs, "RETRIEVAL_HYBRID_ENABLED", True)
        monkeypatch.setattr(rs, "RETRIEVAL_SPARSE_ENABLED", False)
        monkeypatch.setattr(rs, "RETRIEVAL_HYBRID_RERANK", False)
        monkeypatch.setattr(
            cs, "retrieve_docs_with_retry", lambda *a, **kw: _make_docs([("a", "甲", 0.9)])
        )

        redis = FakeRedis()
        await cs.retrieve_scoped_docs_cached(
            kb_id="kb1",
            question="问",
            search_k=3,
            metadata_filter=None,
            vectorstore=object(),
            cache_redis=redis,
            artifact_id="art1",
            scope_token="tok",
            session=object(),
        )
        assert redis.keys and redis.keys[0].endswith("|h1")
        assert redis.payload is not None and redis.payload[0].endswith("|h1")

    @pytest.mark.asyncio
    async def test_stage_report_contextvar_handoff(self):
        from agentix.retrieval.usecases.retrieval_hybrid import (
            pop_stage_report,
            set_stage_report,
        )

        set_stage_report(None)
        assert pop_stage_report() is None
        set_stage_report({"stages": []})
        assert pop_stage_report() == {"stages": []}
        assert pop_stage_report() is None  # 幂等
        set_stage_report(None)

    @pytest.mark.asyncio
    async def test_sse_emits_four_stage_frame_only_when_report_present(self, monkeypatch):
        """R-P1-5：预检产出报告 → 追加 detail.stages 帧；无报告（flag-off）→ 三段帧。"""
        from agentix.retrieval.usecases import chat_service as cs
        from agentix.retrieval.usecases import chat_service_helpers as helpers
        from agentix.retrieval.usecases.retrieval_hybrid import set_stage_report

        class _FakeMsg:
            id = 42

        def _harness(monkeypatch, preflight):
            monkeypatch.setattr(
                cs,
                "_prepare_conversation_and_scope",
                lambda *a, **kw: ("cid-1", _FakeMsg(), "kb", "kb-1"),
            )
            monkeypatch.setattr(
                cs, "_ensure_kb_ready_or_reject", lambda *a, **kw: (object(), None, None, None)
            )
            monkeypatch.setattr(cs, "count_documents_in_scope", lambda *a, **kw: 3)

            async def fake_prepare_context(*a, **kw):
                return {"history_text": "", "scope_label": ""}

            async def fake_prelude(*a, **kw):
                return
                yield  # pragma: no cover

            async def fake_final(*a, **kw):
                yield {"type": "answer_chunk", "content": "答"}
                yield {"type": "finished", "finished": True}

            monkeypatch.setattr(helpers, "_prepare_context", fake_prepare_context)
            monkeypatch.setattr(helpers, "_stream_thinking_prelude_if_deep", fake_prelude)
            monkeypatch.setattr(helpers, "_stream_retrieval_preflight", preflight)
            monkeypatch.setattr(helpers, "_stream_final_answer", fake_final)

        stages = [
            {"stage": "rewrite", "ms": 0.1},
            {"stage": "dense", "ms": 2.0, "hits": 5, "topk": "a,b"},
            {"stage": "sparse", "ms": 1.0, "hits": 3, "topk": "b,z"},
            {"stage": "rrf", "ms": 0.1, "hits": 7, "method": "rrf"},
            {"stage": "rerank", "ms": 5.0, "hits": 7, "breaker": "closed", "applied": True},
        ]

        async def preflight_with_report(*a, **kw):
            set_stage_report({"stages": stages, "fusion_method": "rrf"})
            return None

        _harness(monkeypatch, preflight_with_report)
        events = [
            p
            async for p in cs._stream_answer_events_impl(
                session=object(),
                kb_id="kb-1",
                question="q",
                conversation_id=None,
                request_id="req-1",
                user_id="u-1",
                cache_redis=None,
                model_name="m",
            )
        ]
        progress = [e for e in events if e.get("type") == "progress"]
        assert [p["stage"] for p in progress] == [
            "loading",
            "retrieval",
            "retrieval",
            "llm_generate",
        ]
        breakdown = progress[2]
        assert breakdown["detail"]["step"] == 2 and breakdown["detail"]["total"] == 3
        assert breakdown["detail"]["stages"] == stages
        assert progress[0]["detail"] == {"step": 1, "total": 3}  # 其余帧形状不变

        # 无报告（flag-off 常态）→ 仍是三段帧
        async def preflight_plain(*a, **kw):
            return None

        _harness(monkeypatch, preflight_plain)
        events2 = [
            p
            async for p in cs._stream_answer_events_impl(
                session=object(),
                kb_id="kb-1",
                question="q",
                conversation_id=None,
                request_id="req-2",
                user_id="u-1",
                cache_redis=None,
                model_name="m",
            )
        ]
        progress2 = [e for e in events2 if e.get("type") == "progress"]
        assert [p["stage"] for p in progress2] == ["loading", "retrieval", "llm_generate"]
        for p in progress2:
            assert set(p["detail"]) == {"step", "total"}


# ---------------------------------------------------------------- 6. Channel B（R-P1-2 第二半）


class TestChannelBFusion:
    @pytest.fixture
    def _node_env(self, monkeypatch):
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_nodes import (
            local_search_node as lsn,
        )

        pg = [
            {"id": "p1", "content": "同内容条", "similarity": 0.9, "tool": "postgres"},
            {"id": "p2", "content": "pg独有", "similarity": 0.7, "tool": "postgres"},
        ]
        mv = [
            {"id": "m1", "content": "同内容条", "score": 0.4, "tool": "milvus"},
            {"id": "m2", "content": "mv独有", "score": 0.99, "tool": "milvus"},
        ]

        async def fake_hybrid_retrieve(question, *, cfg, knowledge_service, tools):
            return {"postgres": list(pg), "milvus": list(mv), "failures": [], "degraded": False}

        monkeypatch.setattr(lsn, "hybrid_retrieve", fake_hybrid_retrieve)
        return lsn, pg, mv

    @pytest.mark.asyncio
    async def test_flag_off_postgres_first_unchanged(self, monkeypatch, _node_env):
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_states import (
            KBWorkflowConfig,
        )
        from agentix.config.settings import settings

        lsn, pg, mv = _node_env
        monkeypatch.setattr(settings, "KB_LOCAL_FUSION_ENABLED", False)
        node = lsn.create_local_search_node(knowledge_service=None, cfg=KBWorkflowConfig(top_k=5))
        out = await node({"question": "什么是合同?", "kb_tools": ["postgres", "milvus"]})
        # 旧行为逐位不变：postgres 非空 → 只用 postgres（丢掉 milvus 分）
        assert out["local_results"] == pg
        assert out["degraded"] is False

    @pytest.mark.asyncio
    async def test_flag_on_fuses_both_channels_dedup_by_content(self, monkeypatch, _node_env):
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_states import (
            KBWorkflowConfig,
        )
        from agentix.config.settings import settings

        lsn, pg, mv = _node_env
        monkeypatch.setattr(settings, "KB_LOCAL_FUSION_ENABLED", True)
        node = lsn.create_local_search_node(knowledge_service=None, cfg=KBWorkflowConfig(top_k=3))
        out = await node({"question": "什么是合同?", "kb_tools": ["postgres", "milvus"]})
        contents = [i["content"] for i in out["local_results"]]
        assert len(contents) == 3
        assert contents.count("同内容条") == 1  # 跨通道同内容去重
        same = next(i for i in out["local_results"] if i["content"] == "同内容条")
        assert same["tool"] == "postgres"  # 重复取 postgres 侧（结构化字段更全）
        assert {"pg独有", "mv独有"} <= set(contents)  # 两路命中都保留（不再丢分）

    @pytest.mark.asyncio
    async def test_flag_on_single_channel_passthrough(self, monkeypatch):
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_nodes import (
            local_search_node as lsn,
        )
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_states import (
            KBWorkflowConfig,
        )
        from agentix.config.settings import settings

        monkeypatch.setattr(settings, "KB_LOCAL_FUSION_ENABLED", True)

        async def only_milvus(question, *, cfg, knowledge_service, tools):
            return {
                "postgres": [],
                "milvus": [{"id": "m1", "content": "唯一"}],
                "failures": [],
                "degraded": False,
            }

        monkeypatch.setattr(lsn, "hybrid_retrieve", only_milvus)
        node = lsn.create_local_search_node(knowledge_service=None, cfg=KBWorkflowConfig(top_k=5))
        out = await node({"question": "q?", "kb_tools": ["postgres", "milvus"]})
        assert out["local_results"] == [{"id": "m1", "content": "唯一"}]

    def test_fuse_local_channels_zscore_method(self, monkeypatch):
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_nodes import (
            local_search_node as lsn,
        )
        from agentix.config.settings import settings

        monkeypatch.setattr(settings, "KB_LOCAL_FUSION_METHOD", "zscore")
        pg = [{"id": "p1", "content": "甲", "rerank_score": 0.9}]
        mv = [{"id": "m1", "content": "乙", "score": 0.1}]
        out = lsn.fuse_local_channels(pg, mv, top_k=2)
        assert [i["content"] for i in out] == ["甲", "乙"]
        monkeypatch.setattr(settings, "KB_LOCAL_FUSION_METHOD", "rrf")
