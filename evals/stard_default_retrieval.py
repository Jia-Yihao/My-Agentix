"""Optimize and report the single Dense + BM25/z-score retrieval strategy.

Hyperparameters are selected only on a fixed train-only validation split.
The official dev split is evaluated once afterward as the final held-out
comparison against the current project baseline.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentix.domain.policies.rank_fusion import zscore_fuse  # noqa: E402
from agentix.retrieval.infra.rag.sparse import BM25Index  # noqa: E402
from evals.stard_eval import DATA, evaluate, fetch, load, subset_rows  # noqa: E402

DEFAULT_ALPHA = 0.85
DEFAULT_CANDIDATES = 80
ALPHAS = (0.65, 0.70, 0.75, 0.80, 0.825, 0.85, 0.875, 0.90, 0.925, 0.95, 1.0)
CHANNEL_DEPTHS = (40, 60, 80, 100, 150, 200)
METRIC_KEYS = ("recall@1", "recall@5", "recall@10", "recall@20", "mrr", "ndcg@5")
VAL_SEED = 42
VAL_SIZE = 300
CORPUS_SIZE = 8000
EMBEDDING_CACHE = DATA / "emb_8000_text-embedding-bge-m3.npz"
EMBEDDING_URL = "http://127.0.0.1:1234/v1/embeddings"
EMBEDDING_MODEL = "text-embedding-bge-m3"


def _embed(texts: list[str], *, url: str, model: str, batch_size: int = 96) -> np.ndarray:
    vectors: list[list[float]] = []
    for offset in range(0, len(texts), batch_size):
        payload = json.dumps(
            {"model": model, "input": texts[offset : offset + batch_size]}
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            data = json.load(response)["data"]
        data.sort(key=lambda row: row["index"])
        vectors.extend(row["embedding"] for row in data)
        print(f"[embedding] {min(offset + batch_size, len(texts))}/{len(texts)}", flush=True)

    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[1] != 1024:
        raise ValueError(f"expected 1024-dimensional embeddings, got {matrix.shape}")
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
    return matrix


def prepare_embeddings(*, url: str, model: str) -> None:
    fetch()
    rows, _queries, train, dev = load()
    gold = {**train, **dev}
    corpus = subset_rows(rows, gold, CORPUS_SIZE, seed=VAL_SEED)
    qids = sorted(gold)
    docs = _embed([row[2][:800] for row in corpus], url=url, model=model)
    queries = _embed([_queries[qid][:400] for qid in qids], url=url, model=model)
    EMBEDDING_CACHE.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        EMBEDDING_CACHE,
        doc=docs.astype(np.float16),
        q=queries.astype(np.float16),
        qids=np.asarray(qids),
    )
    print(f"[saved] {EMBEDDING_CACHE}", flush=True)


class StardBenchmark:
    """Single fixed STARD corpus with a train-only validation and official dev split."""

    def __init__(self) -> None:
        rows, self.qs, self.train, self.dev = load()
        self.gold = {**self.train, **self.dev}
        self.rows = subset_rows(rows, self.gold, CORPUS_SIZE, seed=VAL_SEED)
        if len(self.rows) != CORPUS_SIZE:
            raise ValueError(
                f"STARD controlled corpus has {len(self.rows)} docs; expected {CORPUS_SIZE}"
            )
        self.ids = [row[0] for row in self.rows]
        self.qids = sorted(self.gold)
        self.qindex = {qid: index for index, qid in enumerate(self.qids)}

        with np.load(EMBEDDING_CACHE, allow_pickle=False) as embeddings:
            docs = np.asarray(embeddings["doc"], dtype=np.float32)
            queries = np.asarray(embeddings["q"], dtype=np.float32)
            cached_qids = [int(qid) for qid in embeddings["qids"]]
        expected_doc_shape = (CORPUS_SIZE, 1024)
        expected_query_shape = (len(self.qids), 1024)
        if docs.shape != expected_doc_shape or queries.shape != expected_query_shape:
            raise ValueError(
                f"STARD embedding cache shape mismatch: {docs.shape} / {queries.shape}"
            )
        if cached_qids != self.qids:
            raise ValueError("STARD embedding cache query order does not match qrels")
        self.scores = queries @ docs.T

        train_qids = sorted(self.train)
        shuffled = list(train_qids)
        import random

        random.Random(VAL_SEED).shuffle(shuffled)
        self.validation_qids = sorted(shuffled[:VAL_SIZE])
        self.fit_qids = sorted(shuffled[VAL_SIZE:])

    def split_qids(self, split: str) -> tuple[list[int], list[int]]:
        if split == "validation":
            return list(self.validation_qids), list(self.fit_qids)
        if split == "dev":
            return sorted(self.dev), sorted(self.train)
        raise ValueError(f"unsupported STARD split: {split}")


def _rank(
    lab: StardBenchmark,
    qids: list[int],
    bm25: dict[int, list[tuple[str, float]]],
    *,
    alpha: float,
    channel_depth: int,
) -> dict[int, list[str]]:
    ranked: dict[int, list[str]] = {}
    for qid in qids:
        row = lab.scores[lab.qindex[qid]]
        dense_idx = np.argpartition(-row, channel_depth - 1)[:channel_depth]
        dense_idx = dense_idx[np.argsort(-row[dense_idx], kind="stable")]
        dense_channel = [
            (lab.ids[int(idx)], float(row[int(idx)])) for idx in dense_idx
        ]
        fused = zscore_fuse(
            [dense_channel, bm25[qid][:channel_depth]],
            alpha=alpha,
            top_n=channel_depth,
        )
        ranked[qid] = [doc_id for doc_id, _score in fused[:20]]
    return ranked


def _evaluate(
    lab: StardBenchmark,
    qids: list[int],
    bm25: dict[int, list[tuple[str, float]]],
    *,
    alpha: float,
    channel_depth: int,
) -> dict[str, Any]:
    ranked = _rank(
        lab, qids, bm25, alpha=alpha, channel_depth=channel_depth
    )
    metrics = evaluate(ranked, lab.gold, qids, k=5)
    return {
        "queries": len(qids),
        "alpha": alpha,
        "channel_depth": channel_depth,
        "metrics": {key: metrics[key] for key in METRIC_KEYS},
    }


def _selection_key(row: dict[str, Any]) -> tuple[float, ...]:
    metrics = row["metrics"]
    # Prioritize the final top-20 objective, then first-page quality and rank.
    return (
        metrics["recall@20"],
        metrics["recall@5"],
        metrics["mrr"],
        metrics["ndcg@5"],
        -row["channel_depth"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prepare-embeddings",
        action="store_true",
        help="download canonical STARD data and build the single fixed embedding cache",
    )
    parser.add_argument("--embedding-url", default=EMBEDDING_URL)
    parser.add_argument("--embedding-model", default=EMBEDDING_MODEL)
    parser.add_argument(
        "--write-summary",
        type=Path,
        default=Path("docs/evals/stard-default-retrieval.json"),
    )
    args = parser.parse_args()

    if args.prepare_embeddings:
        prepare_embeddings(url=args.embedding_url, model=args.embedding_model)
        return

    lab = StardBenchmark()
    val_qids, _fit = lab.split_qids("validation")
    dev_qids, _train = lab.split_qids("dev")
    index_started = time.perf_counter()
    bm25_index = BM25Index([(row[0], row[2], {}) for row in lab.rows])
    index_build_seconds = time.perf_counter() - index_started
    max_depth = max(CHANNEL_DEPTHS)
    eval_qids = sorted(set(val_qids) | set(dev_qids))
    bm25 = {
        qid: bm25_index.search(lab.qs[qid][:400], top_n=max_depth)
        for qid in eval_qids
    }

    validation_rows = [
        _evaluate(
            lab,
            val_qids,
            bm25,
            alpha=alpha,
            channel_depth=depth,
        )
        for depth in CHANNEL_DEPTHS
        for alpha in ALPHAS
    ]
    validation_rows.sort(key=_selection_key, reverse=True)
    selected = validation_rows[0]
    baseline_val = _evaluate(
        lab,
        val_qids,
        bm25,
        alpha=DEFAULT_ALPHA,
        channel_depth=DEFAULT_CANDIDATES,
    )

    # Selection is now locked using train-only validation. Do not use dev to
    # adjust alpha or channel depth; report only the locked baseline comparison.
    dev_baseline = _evaluate(
        lab,
        dev_qids,
        bm25,
        alpha=DEFAULT_ALPHA,
        channel_depth=DEFAULT_CANDIDATES,
    )
    dev_selected = _evaluate(
        lab,
        dev_qids,
        bm25,
        alpha=selected["alpha"],
        channel_depth=selected["channel_depth"],
    )
    report = {
        "schema": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dataset": "STARD, single canonical corpus: 8,000-document controlled subset",
        "corpus": {
            "documents": len(lab.rows),
            "construction": "all judged relevant documents plus seed=42 sampled distractors",
            "embedding_model": "text-embedding-bge-m3",
        },
        "metric_definition": {
            "judgement": "Exact qrels docid",
            "recall": (
                "Macro-average fraction of all relevant qrels docids retrieved in top K "
                "per query; exact docid matching."
            ),
            "not_accuracy": (
                "Not classification accuracy or query hit rate; each query contributes "
                "its fraction of all relevant documents retrieved."
            ),
            "implementation": "evals/stard_eval.py::evaluate",
        },
        "protocol": {
            "selection": "300-query train-only validation split (seed=42); remaining 935 train queries are excluded from selection",
            "final_holdout": "official dev split; evaluated only after selection is fixed",
            "output_k": 20,
            "candidate_depth_grid": list(CHANNEL_DEPTHS),
            "alpha_grid": list(ALPHAS),
            "fusion": "z-score normalized raw Dense cosine similarity + raw BM25 score",
            "bm25_index_build_seconds_local": round(index_build_seconds, 4),
        },
        "split": {
            "train": len(lab.train),
            "validation": len(val_qids),
            "dev": len(dev_qids),
        },
        "config_fingerprint": {
            "fusion": "zscore",
            "alpha": DEFAULT_ALPHA,
            "candidate_depth": DEFAULT_CANDIDATES,
            "search_k": 20,
        },
        "reproduce": (
            ".venv/Scripts/python.exe evals/stard_default_retrieval.py "
            f"--write-summary {args.write_summary.as_posix()}"
        ),
        "current_project_baseline": {
            "alpha": DEFAULT_ALPHA,
            "channel_depth": DEFAULT_CANDIDATES,
            "validation": baseline_val,
            "dev": dev_baseline,
        },
        "selected_on_validation": {
            "alpha": selected["alpha"],
            "channel_depth": selected["channel_depth"],
            "validation": selected,
        },
        "locked_dev_comparison": {
            "baseline": dev_baseline,
            "selected": dev_selected,
            "delta": {
                key: round(
                    dev_selected["metrics"][key] - dev_baseline["metrics"][key], 6
                )
                for key in METRIC_KEYS
            },
        },
        "production_decision": (
            "Keep Dense + BM25/z-score as the only retrieval strategy. The alpha and "
            "candidate depth selected on train-only validation are reported separately "
            "from the fixed alpha=0.85, depth=80 project baseline."
        ),
        "validation_grid_ranked": validation_rows,
        "caveats": [
            "All quality comparisons use this one controlled 8,000-document corpus.",
            "The controlled subset contains all judged relevant documents; absolute scores do not estimate full-corpus performance.",
            "The dev comparison is a final holdout comparison and must not be used to retune the selected parameters.",
            "BM25 queries are capped at 400 characters to match the runtime retrieval path.",
            "Local in-process BM25 index timing excludes database loading and production network overhead.",
        ],
    }
    args.write_summary.parent.mkdir(parents=True, exist_ok=True)
    args.write_summary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "dataset": report["dataset"],
                "validation_baseline": baseline_val,
                "selected_validation": selected,
                "dev_baseline": dev_baseline,
                "dev_selected": dev_selected,
                "dev_delta": report["locked_dev_comparison"]["delta"],
                "saved": str(args.write_summary),
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
