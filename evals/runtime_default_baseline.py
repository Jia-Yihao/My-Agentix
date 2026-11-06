"""项目统一默认链路端到端基线：质量 + 六层延迟 + 调用面 + 配置指纹。

与 ``evals/stard_eval.py`` 的边界：后者只报「某检索形态在某语料上的离线排序质量」；
本文件把**项目统一默认配置**当作被测对象，逐阶段计时跑完整条链：

    cache_read → embedding → vector_retrieval → fusion → rerank →
    citation_hydration → cache_write

项目统一默认配置画像（从 ``agentix.config.settings`` 经兼容层运行时读值，不手写）：

    hybrid=true, sparse=true, fusion=zscore(alpha=0.85),
    dense_rerank=false, hybrid_rerank=false,
    query_memory=false, semantic_cache=false, singleflight=false, adaptive=false,
    candidate_pool = min(FUSION_TOP_N, search_k * 4),
    embedding_model=text-embedding-bge-m3,
    rerank_model=text-embedding-bge-reranker-v2-m3（未调用）

诚实边界（写进生成物 ``measurement_scope``，不得对外包装成线上真实读数）：

- 语料是 STARD 受控子集 8,000 条（全部金标 + seed=42 干扰），向量是
  ``.tmp/stard/emb_8000_text-embedding-bge-m3.npz`` 预计算缓存；不是线上 PGVector 库。
- embedding 层是**真实** LM Studio HTTP 调用（逐题，带重试计数）；其余层的
  墙钟是本机 ``perf_counter`` 真值，向量检索为 8000x1024 点积（线上等价计算形态，
  不含 PGVector 网络 RTT），引用还原走真实 ``document_repo`` 批量函数 + 内存 SQLite，
  缓存读写走真实 ``chat_service`` 序列化函数 + 进程内 dict（不含 Redis 网络 RTT）。
- ``cold`` = 首次全 miss；``warm`` = 同键重放全命中。两趟都报，不合成一个数。
- 质量指标用 ``evals.stard_eval.evaluate``（同一打分口径，不另写一份），
  并同时报默认链实际交付的 top-k 与「canonical pool=100」两套，避免不同候选池深度下的
  MRR 被误读。

用法（Git Bash 绝对路径风格）：

  .venv/Scripts/python.exe evals/runtime_default_baseline.py --split dev \
      --write-summary docs/evals/stard-runtime-default-baseline.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentix.retrieval.settings import settings  # noqa: E402
from evals.stard_eval import DATA, POOL, evaluate, load, subset_rows  # noqa: E402
from evals.stard_summary_governance import annotate_runtime_default  # noqa: E402

EMB_CACHE = DATA / "emb_8000_text-embedding-bge-m3.npz"
EMBEDDING_URL = "http://127.0.0.1:1234/v1/embeddings"
SEARCH_K = int(settings.KB_TOP_K)
SEED = 42
SUBSET = 8000

LAYERS = (
    "embedding",
    "vector_retrieval",
    "fusion",
    "rerank",
    "citation_hydration",
    "cache_io",
)

# 项目统一默认画像的期望值：任何一项与运行时 settings 不符都直接拒绝出数，
# 避免「以为在测项目统一默认、其实环境里某个 flag 被打开」而不自知。
EXPECTED_FLAGS: dict[str, bool] = {
    "hybrid": True,
    "sparse": True,
    "dense_rerank": False,
    "hybrid_rerank": False,
    "query_memory": False,
    "semantic_cache": False,
    "singleflight": False,
    "adaptive": False,
    "reranker_enabled": False,
}


def _config_fingerprint() -> dict[str, Any]:
    """从真实 settings 读值组装配置指纹，并强制校验项目统一默认画像。"""
    fp: dict[str, Any] = {
        "hybrid": bool(settings.RETRIEVAL_HYBRID_ENABLED),
        "sparse": bool(settings.RETRIEVAL_SPARSE_ENABLED),
        "dense_rerank": bool(settings.RETRIEVAL_DENSE_RERANK_ENABLED),
        "hybrid_rerank": bool(settings.RETRIEVAL_HYBRID_RERANK),
        "query_memory": bool(settings.RETRIEVAL_QUERY_MEMORY_ENABLED),
        "semantic_cache": bool(settings.SEMANTIC_CACHE_ENABLED),
        "singleflight": bool(settings.RETRIEVAL_SINGLEFLIGHT_ENABLED),
        "adaptive": bool(settings.RETRIEVAL_ADAPTIVE_ENABLED),
        "reranker_enabled": bool(settings.RERANKER_ENABLED),
        "rerank_model": str(settings.RERANKER_MODEL),
        "embedding_model": str(settings.EMBEDDING_MODEL),
        "candidate_pool": max(
            SEARCH_K,
            min(int(settings.RETRIEVAL_FUSION_TOP_N), SEARCH_K * 4),
        ),
        "search_k": SEARCH_K,
        "fusion": (
            f"{settings.RETRIEVAL_FUSION_METHOD} (dense+bm25, alpha={settings.RETRIEVAL_FUSION_ALPHA})"
            if settings.RETRIEVAL_FUSION_METHOD == "zscore"
            else f"{settings.RETRIEVAL_FUSION_METHOD} (dense+bm25)"
        ),
        "settings_source": "agentix/config/settings.py via retrieval settings compatibility wrapper",
    }
    drift = {k: (fp[k], v) for k, v in EXPECTED_FLAGS.items() if fp[k] != v}
    if drift:
        raise SystemExit(f"project-default profile drifted, refuse to report: {drift}")
    canonical = json.dumps(fp, ensure_ascii=False, sort_keys=True)
    fp["sha1"] = hashlib.sha1(  # nosec B324 - 非安全用途：配置指纹
        canonical.encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:12]
    return fp


def _load_corpus() -> tuple[list[tuple[str, str, str]], dict, dict, dict, np.ndarray, np.ndarray, list[int]]:
    """复用 stard_eval 的语料构造与既有向量缓存，保证口径逐位一致。"""
    rows, qs, train, dev = load()
    gold = {**train, **dev}
    subset = subset_rows(rows, gold, SUBSET, SEED)
    if len(subset) != SUBSET:
        raise SystemExit(f"subset rows mismatch: {len(subset)} != {SUBSET}")
    z = np.load(EMB_CACHE, allow_pickle=False)
    D = np.asarray(z["doc"], dtype=np.float32)
    Q = np.asarray(z["q"], dtype=np.float32)
    cached_qids = [int(x) for x in z["qids"]]
    if D.shape != (SUBSET, 1024) or Q.shape != (1543, 1024):
        raise SystemExit(f"embedding cache shape mismatch: {D.shape} / {Q.shape}")
    if cached_qids != list(range(1543)):
        raise SystemExit("embedding cache qid order unexpected")
    return subset, qs, train, dev, D, Q, cached_qids


def _top_ids(sims: np.ndarray, ids: list[str], k: int) -> list[str]:
    """按分数降序取 top-k docid，与 stard_eval.semantic_rank 同一取法。"""
    k = min(k, len(ids))
    part = np.argpartition(-sims, k - 1)[:k]
    part = part[np.argsort(-sims[part], kind="stable")]
    return [ids[int(i)] for i in part]


def _latency_pcts(values: list[float]) -> dict[str, float]:
    """P50/P95/P99，单位毫秒；分位口径委托 ``evals/perf_stats``。"""
    from evals.perf_stats import floor_rank_quantile

    if not values:
        return {"n": 0, "p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0}

    def at(q: float) -> float:
        return round(floor_rank_quantile(values, q) * 1000.0, 4)

    return {
        "n": len(values),
        "p50_ms": at(0.50),
        "p95_ms": at(0.95),
        "p99_ms": at(0.99),
    }


def _live_embed(text: str, counters: dict[str, int]) -> np.ndarray:
    """真实上游 embedding 调用；重试次数与下游调用次数一并记账。"""
    payload = json.dumps(
        {"model": settings.EMBEDDING_MODEL, "input": [text]}
    ).encode("utf-8")
    req = urllib.request.Request(
        EMBEDDING_URL,
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": "Bearer lm-studio"},
    )
    body: dict | None = None
    for attempt in range(3):
        counters["embedding_upstream_calls"] += 1
        if attempt:
            counters["retries"] += 1
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.load(resp)
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(0.5)
    if not body or "data" not in body or not body["data"]:
        raise RuntimeError("embedding response missing data")
    vec = np.asarray(body["data"][0]["embedding"], dtype=np.float32)
    vec /= np.linalg.norm(vec) + 1e-9
    return vec


def _build_sqlite_mirror(doc_ids: list[str]) -> tuple[Any, Any]:
    """用真实生产 ORM 建内存 SQLite 镜像，供引用还原层跑真 SQL。"""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    from sqlmodel import Session, SQLModel, create_engine

    from agentix.retrieval.domain.models.document import (
        Chunk,
        Document,
        FileObject,
        KbFile,
    )
    from agentix.retrieval.infra.repos import document_repo

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(
        engine,
        tables=[Document.__table__, FileObject.__table__, KbFile.__table__, Chunk.__table__],
    )
    unique = list(dict.fromkeys(str(x) for x in doc_ids))
    with Session(engine) as session:
        file_objects = [
            FileObject(
                object_key=f"k{d}", sha256=f"h{d}", size_bytes=1024, file_type="txt"
            )
            for d in unique
        ]
        session.add_all(file_objects)
        session.flush()
        kb_files = [
            KbFile(
                kb_id="kb_stard",
                file_object_id=fo.id,
                original_filename=f"stard_{d}.txt",
                uploaded_by="runtime_default_baseline",
            )
            for d, fo in zip(unique, file_objects, strict=True)
        ]
        session.add_all(kb_files)
        session.flush()
        documents = [
            Document(
                kb_id="kb_stard",
                kb_file_id=kbf.id,
                title=f"法条 {d}",
                status="ready",
                folder_path="/stard",
            )
            for d, kbf in zip(unique, kb_files, strict=True)
        ]
        session.add_all(documents)
        session.flush()
        chunks = [
            Chunk(
                id=f"c{d}",
                document_id=doc.id,
                content=f"法条 {d} 正文" * 20,
                page_no=1,
            )
            for d, doc in zip(unique, documents, strict=True)
        ]
        session.add_all(chunks)
        session.commit()

    counters: dict[str, int] = {"sql": 0}

    @event.listens_for(Engine, "before_cursor_execute")
    def _count(_conn, _cursor, _statement, _parameters, _context, _executemany):
        counters["sql"] += 1

    return (engine, Session), (document_repo, counters, event, Engine)


def _make_docs(top: list[str], id2text: dict[str, str]) -> list[Any]:
    from langchain_core.documents import Document as LCDocument

    return [
        LCDocument(
            page_content=id2text[d][:512],
            metadata={"doc_id": d, "chunk_id": f"c{d}"},
        )
        for d in top
    ]


def _run_pass(
    *,
    qids: list[int],
    qs: dict,
    D: np.ndarray,
    Q: np.ndarray,
    ids: list[str],
    id2text: dict[str, str],
    bm25_index: Any,
    engine_session: tuple[Any, Any],
    mirror: tuple[Any, Any],
    cache: dict[str, bytes],
    live_embedding: bool,
) -> dict[str, Any]:
    """跑一趟完整链；返回逐层延迟、质量排名、调用面与可靠性计数。"""
    from agentix.retrieval.usecases.chat_service import (
        _deserialize_docs_from_cache,
        _serialize_docs_for_cache,
    )

    _engine, SessionCls = engine_session
    document_repo, sql_counter, _event, _Engine = mirror
    layers: dict[str, list[float]] = {name: [] for name in LAYERS}
    totals: list[float] = []
    cache_io_detail: dict[str, list[float]] = {"read": [], "write": [], "deserialize": []}
    served: dict[int, list[str]] = {}
    canonical: dict[int, list[str]] = {}
    counters: dict[str, int] = {
        "embedding_upstream_calls": 0,
        "vector_retrieval_calls": 0,
        "fusion_calls": 0,
        "rerank_calls": 0,
        "cache_get": 0,
        "cache_set": 0,
        "cache_hits": 0,
        "cache_misses": 0,
        "db_sql_statements": 0,
        "retries": 0,
        "breaker_open": 0,
        "failopen": 0,
        "errors": 0,
        "cosine_checks": 0,
    }
    cosine_deltas: list[float] = []

    with SessionCls(_engine) as session:
        for qid in qids:
            key = f"agentix_retrieval:retrieve:stard|{qid}|k{SEARCH_K}|h1"
            started = time.perf_counter()

            # 1) 缓存读（精确键）
            t0 = time.perf_counter()
            raw = cache.get(key)
            cache_io_detail["read"].append(time.perf_counter() - t0)
            counters["cache_get"] += 1

            if raw is not None:
                counters["cache_hits"] += 1
                t0 = time.perf_counter()
                docs = _deserialize_docs_from_cache(raw)
                cache_io_detail["deserialize"].append(time.perf_counter() - t0)
                for name in ("embedding", "vector_retrieval", "fusion", "rerank", "citation_hydration"):
                    layers[name].append(0.0)
                layers["cache_io"].append(
                    cache_io_detail["read"][-1] + cache_io_detail["deserialize"][-1]
                )
                cache_io_detail["write"].append(0.0)
                totals.append(time.perf_counter() - started)
                served[qid] = [str(d.metadata.get("doc_id", "")) for d in (docs or [])]
                canonical[qid] = served[qid]
                continue

            counters["cache_misses"] += 1

            # 2) embedding：真实上游调用；flag 关时也可用缓存向量（仅用于无服务端复算）
            t0 = time.perf_counter()
            try:
                if live_embedding:
                    qvec = _live_embed(qs[qid][:400], counters)
                else:
                    qvec = Q[qid].astype(np.float32)
                    qvec /= np.linalg.norm(qvec) + 1e-9
                if live_embedding:
                    cosine_deltas.append(
                        float(np.dot(qvec, Q[qid]) / (np.linalg.norm(Q[qid]) + 1e-9))
                    )
                    counters["cosine_checks"] += 1
            except Exception as exc:  # noqa: BLE001 - 上游失败按 fail-open 计入可靠性面
                counters["errors"] += 1
                counters["failopen"] += 1
                qvec = Q[qid].astype(np.float32)
            layers["embedding"].append(time.perf_counter() - t0)

            # 3) Dense/BM25 两路召回，通道深度沿用线上 hybrid 的 4x search_k 上限。
            channel_n = max(
                SEARCH_K,
                min(int(settings.RETRIEVAL_FUSION_TOP_N), SEARCH_K * 4),
            )
            t0 = time.perf_counter(            )
            sims = D @ qvec
            dense_order = np.argpartition(-sims, channel_n - 1)[:channel_n]
            dense_order = dense_order[np.argsort(-sims[dense_order], kind="stable")]
            dense_channel = [
                (ids[int(i)], float(sims[int(i)])) for i in dense_order
            ]
            dense_docs = [doc_id for doc_id, _score in dense_channel]
            counters["vector_retrieval_calls"] += 1
            layers["vector_retrieval"].append(time.perf_counter() - t0)

            # 4) BM25 补充召回与配置指定的分数融合。
            t0 = time.perf_counter()
            from agentix.domain.policies.rank_fusion import rrf_fuse

            sparse_channel = [
                (doc_id, float(score))
                for doc_id, score in bm25_index.search(qs[qid][:400], top_n=channel_n)
            ]
            if settings.RETRIEVAL_FUSION_METHOD == "zscore":
                from agentix.domain.policies.rank_fusion import zscore_fuse

                fused_pairs = zscore_fuse(
                    [dense_channel, sparse_channel],
                    alpha=float(settings.RETRIEVAL_FUSION_ALPHA),
                    top_n=int(settings.RETRIEVAL_FUSION_TOP_N),
                )
            else:
                fused_pairs = rrf_fuse(
                    [dense_docs, [doc_id for doc_id, _score in sparse_channel]],
                    k=int(settings.RETRIEVAL_RRF_K),
                    top_n=int(settings.RETRIEVAL_FUSION_TOP_N),
                )
            fused = [doc_id for doc_id, _score in fused_pairs]
            counters["fusion_calls"] += 1
            layers["fusion"].append(time.perf_counter() - t0)

            # 5) Cross-Encoder 默认关闭；线上实际开启时另行实测服务延迟。
            t0 = time.perf_counter()
            ranked = fused[:SEARCH_K]
            layers["rerank"].append(time.perf_counter() - t0)

            # 6) 引用还原：真实批量 repo 函数 + 真 SQL
            t0 = time.perf_counter()
            docs = _make_docs(ranked, id2text)
            chunk_ids = [str(d.metadata["chunk_id"]) for d in docs]
            sql_counter["sql"] = 0
            chunks = document_repo.get_chunks_by_ids(session, chunk_ids)
            doc_ids = [
                str(c.document_id) for c in chunks.values() if c.document_id
            ]
            metas = document_repo.get_document_file_metas(session, doc_ids)
            counters["db_sql_statements"] += sql_counter["sql"]
            assert len(chunks) <= SEARCH_K and isinstance(metas, dict)
            layers["citation_hydration"].append(time.perf_counter() - t0)

            # 7) 缓存写：真实序列化函数 + 进程内 dict
            t0 = time.perf_counter()
            payload = _serialize_docs_for_cache(docs).encode("utf-8")
            cache[key] = payload
            write_s = time.perf_counter() - t0
            cache_io_detail["write"].append(write_s)
            cache_io_detail["deserialize"].append(0.0)
            counters["cache_set"] += 1
            layers["cache_io"].append(cache_io_detail["read"][-1] + write_s)

            served[qid] = [str(d.metadata["doc_id"]) for d in docs]
            canonical[qid] = fused[:POOL]
            totals.append(time.perf_counter() - started)

    return {
        "layers": {name: _latency_pcts(v) for name, v in layers.items()},
        "total": _latency_pcts(totals),
        "cache_io_detail": {name: _latency_pcts(v) for name, v in cache_io_detail.items()},
        "served": served,
        "canonical": canonical,
        "call_surface": counters,
        "cosine_live_vs_cache": {
            "n": len(cosine_deltas),
            "min": round(min(cosine_deltas), 6) if cosine_deltas else None,
            "mean": round(sum(cosine_deltas) / len(cosine_deltas), 6) if cosine_deltas else None,
        },
        "_totals": totals,
    }


def _quality(ranked: dict[int, list[str]], dev: dict, qids: list[int]) -> dict[str, float]:
    m = evaluate(ranked, dev, qids, k=5)
    return {
        "recall@5": m["recall@5"],
        "recall@20": m["recall@20"],
        "mrr": m["mrr"],
        "ndcg@5": m["ndcg@5"],
        "recall@10": m["recall@10"],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--split", choices=("dev60", "dev", "all"), default="dev")
    ap.add_argument("--live-embedding", action="store_true", help="逐题真实调用 LM Studio（默认开，除非 --no-live-embedding）")
    ap.add_argument("--no-live-embedding", dest="live_embedding", action="store_false")
    ap.set_defaults(live_embedding=True)
    ap.add_argument("--write-summary", type=Path)
    args = ap.parse_args()

    fp = _config_fingerprint()
    subset, qs, train, dev, D, Q, cached_qids = _load_corpus()
    ids = [r[0] for r in subset]
    id2text = {r[0]: r[2] for r in subset}
    gold = {**train, **dev}
    all_qids = sorted(gold)

    # 用缓存 query 向量与 STARD 子集 BM25 先算全量 hybrid 排名（诊断用，不参与
    # 冷启动计时），同时确定引用还原 SQLite 镜像需要覆盖的文档集合。
    S = Q @ D.T
    from agentix.domain.policies.rank_fusion import rrf_fuse, zscore_fuse
    from agentix.retrieval.infra.rag.sparse import BM25Index

    bm25_index = BM25Index([(row[0], row[2], {}) for row in subset])
    channel_n = max(SEARCH_K, min(int(settings.RETRIEVAL_FUSION_TOP_N), SEARCH_K * 4))
    hybrid_rank_all: dict[int, list[str]] = {}
    for i, qid in enumerate(cached_qids):
        dense_order = np.argpartition(-S[i], channel_n - 1)[:channel_n]
        dense_order = dense_order[np.argsort(-S[i, dense_order], kind="stable")]
        dense_channel = [(ids[int(j)], float(S[i, int(j)])) for j in dense_order]
        sparse_channel = [
            (doc_id, float(score))
            for doc_id, score in bm25_index.search(qs[qid][:400], top_n=channel_n)
        ]
        if settings.RETRIEVAL_FUSION_METHOD == "zscore":
            fused_pairs = zscore_fuse(
                [dense_channel, sparse_channel],
                alpha=float(settings.RETRIEVAL_FUSION_ALPHA),
                top_n=int(settings.RETRIEVAL_FUSION_TOP_N),
            )
        else:
            fused_pairs = rrf_fuse(
                [
                    [doc_id for doc_id, _score in dense_channel],
                    [doc_id for doc_id, _score in sparse_channel],
                ],
                k=int(settings.RETRIEVAL_RRF_K),
                top_n=int(settings.RETRIEVAL_FUSION_TOP_N),
            )
        hybrid_rank_all[qid] = [doc_id for doc_id, _score in fused_pairs]
    mirror_doc_ids = list(
        dict.fromkeys(d for q in cached_qids for d in hybrid_rank_all[q][:POOL])
    )
    qids_dev = sorted(dev)
    qids_eval = qids_dev[:60] if args.split == "dev60" else (
        all_qids if args.split == "all" else qids_dev
    )

    # 镜像只需覆盖本次评测 query 的候选文档；引用还原只会命中 served top-k。
    mirror_doc_ids = list(
        dict.fromkeys(d for q in qids_eval for d in hybrid_rank_all[q][:POOL])
    )
    engine_session, mirror = _build_sqlite_mirror(mirror_doc_ids)

    cache: dict[str, bytes] = {}
    cold = _run_pass(
        qids=qids_eval,
        qs=qs,
        D=D,
        Q=Q,
        ids=ids,
        id2text=id2text,
        bm25_index=bm25_index,
        engine_session=engine_session,
        mirror=mirror,
        cache=cache,
        live_embedding=args.live_embedding,
    )
    warm = _run_pass(
        qids=qids_eval,
        qs=qs,
        D=D,
        Q=Q,
        ids=ids,
        id2text=id2text,
        bm25_index=bm25_index,
        engine_session=engine_session,
        mirror=mirror,
        cache=cache,
        live_embedding=False,
    )

    # 质量：served top-k（默认链真实交付）与 canonical pool=100（既有发布口径锚点）
    dev60_qids = qids_dev[:60]
    quality: dict[str, Any] = {
        "served_top_k": {
            "dev": _quality(cold["served"], dev, qids_eval) if args.split != "all" else _quality(cold["served"], gold, qids_eval),
        },
        "canonical_pool100": {
            "dev": _quality({q: hybrid_rank_all[q] for q in qids_dev}, dev, qids_dev),
            "dev60": _quality({q: hybrid_rank_all[q] for q in dev60_qids}, dev, dev60_qids),
            "all": _quality(hybrid_rank_all, gold, all_qids),
        },
    }
    if args.split != "all":
        quality["served_top_k"]["dev60"] = _quality(
            {q: cold["served"][q] for q in qids_eval[:60]}, dev, qids_eval[:60]
        )

    # 调用面：cold/warm 分开，命中率按真实 get 次数算
    def _surface(run: dict[str, Any]) -> dict[str, Any]:
        c = run["call_surface"]
        gets = max(1, c["cache_get"])
        return {
            **c,
            "cache_hit_rate": round(c["cache_hits"] / gets, 6),
            "downstream_calls_total": (
                c["embedding_upstream_calls"]
                + c["vector_retrieval_calls"]
                + c["rerank_calls"]
                + c["db_sql_statements"]
            ),
        }

    report: dict[str, Any] = {
        "schema": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "reproduce": " ".join(
            [".venv/Scripts/python.exe", "evals/runtime_default_baseline.py", *sys.argv[1:]]
        ),
        "profile": "project-default",
        "config_fingerprint": fp,
        "corpus": {
            "name": "STARD 受控子集 8,000 条（全部金标法条 + seed=42 干扰文档）",
            "rows": len(subset),
            "gold_docs": len({d for g in gold.values() for d in g}),
            "full_corpus_rows": 55348,
            "seed": SEED,
            "embedding_cache": str(EMB_CACHE.relative_to(ROOT)).replace("\\", "/"),
            "artifact_note": "embedding_cache 指向 .tmp/ 下的本地缓存，仅本地使用，已由 .gitignore 排除。",
            "boundary": "子集只用于同一子集内的方法间比较，绝对值不可与全库 55,348 互读",
        },
        "split": {
            "requested": args.split,
            "eval_n": len(qids_eval),
            "dev": 308,
            "dev60": 60,
            "train": 1235,
            "all": 1543,
            "blind": True,
            "qids_source": "sorted(qrels.dev.tsv) 与 stard_eval.load 完全一致",
        },
        "metric_definition": {
            "judgement": "精确 docid（qrels），命中定义 = 至少一条相关法条进入 top-k",
            "implementation": "evals/stard_eval.py::evaluate（本文件不另写打分口径）",
            "search_k": SEARCH_K,
            "candidate_pool": fp["candidate_pool"],
            "pool": POOL,
            "pool_note": (
                "canonical_pool100 是既有发布读数的排序表口径（MRR 用候选池内首个命中位置）；"
                f"served_top_k 是默认链实际交付的 {SEARCH_K} 条，其 MRR 受 search_k={SEARCH_K} 截断；"
                "recall@5 / recall@10 / recall@20 / NDCG@5 与 canonical_pool100 一致。"
            ),
        },
        "measurement_scope": {
            "kind": "项目统一默认链路的离线等价复刻（stage-instrumented in-process harness）",
            "not_measured": [
                "PGVector / 真实向量库网络 RTT",
                "线上从 PostgreSQL 加载 BM25 语料与索引构建时间（harness 使用 STARD 子集内存 BM25）",
                "真实 Redis 网络 RTT（进程内 dict + 真实 chat_service 序列化函数）",
                "FastAPI 路由、LLM 生成、流式输出",
                "线上 QPS / 并发（本基线为串行逐题）",
                *([] if args.live_embedding else ["LM Studio 实时 embedding 上游调用与延迟"]),
            ],
            "real_upstream": [
                (
                    "embedding：逐题真实 LM Studio /v1/embeddings 调用（带重试计数）"
                    if args.live_embedding
                    else "embedding：复用本地预计算向量（--no-live-embedding），未调用 LM Studio"
                ),
                "引用还原：真实 document_repo.get_chunks_by_ids / get_document_file_metas + SQLite 真 SQL",
                "缓存读写：真实 chat_service._serialize_docs_for_cache / _deserialize_docs_from_cache",
                "向量检索：8000x1024 真实点积 + top-k（线上等价计算形态）",
                "BM25：同一 8,000 篇 STARD 受控子集的进程内 BM25 检索，索引预先构建",
            ],
            "passes": {
                "cold": "首次全 miss，六层全部执行",
                "warm": "同键重放全命中，只执行 cache_read + 反序列化",
            },
            "latency_unit": "毫秒，time.perf_counter 真值，P50/P95/P99 用最近秩",
        },
        "quality": quality,
        "dense_only_reference": {
            "dev": {
                "recall@5": 0.711039,
                "recall@20": 0.840909,
                "mrr": 0.601445,
                "ndcg@5": 0.545482,
            },
            "note": "Published Dense-only STARD baseline for historical comparison; not the current project default.",
        },
        "latency": {
            "cold": {
                "total": cold["total"],
                "layers": cold["layers"],
                "cache_io_detail": cold["cache_io_detail"],
            },
            "warm": {
                "total": warm["total"],
                "layers": warm["layers"],
                "cache_io_detail": warm["cache_io_detail"],
            },
        },
        "call_surface": {"cold": _surface(cold), "warm": _surface(warm)},
        "reliability": {
            "cold": {
                "breaker_open": cold["call_surface"]["breaker_open"],
                "failopen": cold["call_surface"]["failopen"],
                "retries": cold["call_surface"]["retries"],
                "errors": cold["call_surface"]["errors"],
            },
            "warm": {
                "breaker_open": warm["call_surface"]["breaker_open"],
                "failopen": warm["call_surface"]["failopen"],
                "retries": warm["call_surface"]["retries"],
                "errors": warm["call_surface"]["errors"],
            },
            "note": (
                "项目统一默认 hybrid_rerank=false / dense_rerank=false ⇒ rerank 上游调用 0 次、熔断/fail-open 恒为 0；"
                "此处 0 是真实计数，不是默认值填充。重试只统计 embedding 上游。"
            ),
        },
        "integrity": {
            "live_embedding_vs_cache_cosine": cold["cosine_live_vs_cache"],
            "note": (
                "实调向量与 .tmp/stard 缓存 query 向量的余弦，验证冷路径与质量口径同模型同文本"
                if args.live_embedding
                else "--no-live-embedding：未做实时向量余弦检查，评测质量直接使用本地预计算 query 向量"
            ),
        },
    }

    # 三处口径风险说明由治理模块统一注入，harness 不另写一份转述
    annotate_runtime_default(report)
    report["latency"]["cold"]["layer_notes"]["embedding"] = (
        "逐题真实 LM Studio /v1/embeddings 调用（带重试计数）"
        if args.live_embedding
        else "本次使用本地预计算向量（--no-live-embedding），未测 LM Studio 上游延迟"
    )

    # 质量结果使用统一评测指标；Dense-only 发布基线作为对照而非当前配置锚点。
    got_dev = quality["canonical_pool100"]["dev"]
    got_all = quality["canonical_pool100"]["all"]
    for split_name, metrics in (("dev", got_dev), ("all", got_all)):
        if any(not 0.0 <= float(value) <= 1.0 for value in metrics.values()):
            raise SystemExit(f"invalid {split_name} quality metrics: {metrics}")
    print(json.dumps({
        "profile": "project-default",
        "config_fingerprint_sha1": fp["sha1"],
        "split": args.split,
        "eval_n": len(qids_eval),
        "quality": quality,
        "latency_cold_total_ms": cold["total"],
        "latency_cold_layers_ms": cold["layers"],
        "latency_warm_total_ms": warm["total"],
        "call_surface_cold": report["call_surface"]["cold"],
        "call_surface_warm": report["call_surface"]["warm"],
        "integrity": report["integrity"],
    }, ensure_ascii=False, indent=2), flush=True)

    if args.write_summary:
        args.write_summary.parent.mkdir(parents=True, exist_ok=True)
        args.write_summary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"[saved] {args.write_summary}", flush=True)


if __name__ == "__main__":
    main()
