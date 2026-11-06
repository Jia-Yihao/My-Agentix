"""Canonical STARD data loading and shared retrieval metrics.

The public benchmark uses the official 1,235-query train and 308-query dev
splits with exact qrels docids. Retrieval experiments use one controlled
8,000-document subset and a fixed 300-query validation split selected from
train only.
"""

from __future__ import annotations

import json
import math
import random
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".tmp" / "stard"
REPO = "oneal2000/STARD"
RAW = "https://raw.githubusercontent.com/oneal2000/STARD/main/"
FILES = {
    "queries.json": "data/queries.json",
    "qrels.train.tsv": "data/example/qrels.train.tsv",
    "qrels.dev.tsv": "data/example/qrels.dev.tsv",
    "corpus.tsv": "data/example/corpus.tsv",
}


def fetch() -> None:
    """Fetch missing canonical STARD source files into the ignored local cache."""
    import base64
    import subprocess

    DATA.mkdir(parents=True, exist_ok=True)
    for name, remote in FILES.items():
        target = DATA / name
        if target.exists() and target.stat().st_size > 0:
            continue
        payload = None
        try:
            request = urllib.request.Request(
                RAW + remote, headers={"User-Agent": "stard-eval"}
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read()
        except Exception:
            result = subprocess.run(
                ["gh", "api", f"repos/{REPO}/contents/{remote}", "--jq", ".content"],
                capture_output=True,
                check=True,
            )
            payload = base64.b64decode(result.stdout.decode().strip())
        target.write_bytes(payload)


def load():
    """Load canonical STARD corpus, queries, and official train/dev qrels."""
    rows = []
    with (DATA / "corpus.tsv").open(encoding="utf-8") as corpus:
        for line in corpus:
            parts = line.rstrip("\n").split("\t", 2)
            if len(parts) < 2 or not parts[0].strip().isdigit():
                continue
            title = parts[1].strip()
            body = parts[2].replace("\\n", " ") if len(parts) > 2 else ""
            rows.append((parts[0].strip(), title, f"{title} {body}"))

    queries = {
        row["query_id"]: row["问题"]
        for row in json.loads((DATA / "queries.json").read_text(encoding="utf-8"))
    }

    def read_qrels(filename: str, *, train: bool) -> dict[int, set[str]]:
        qrels: dict[int, set[str]] = {}
        with (DATA / filename).open(encoding="utf-8") as source:
            for line in source:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < (4 if train else 2):
                    continue
                qid = int(parts[0])
                docid = str(int(parts[2])) if train else str(int(parts[1]))
                qrels.setdefault(qid, set()).add(docid)
        return qrels

    return (
        rows,
        queries,
        read_qrels("qrels.train.tsv", train=True),
        read_qrels("qrels.dev.tsv", train=False),
    )


def subset_rows(rows, gold, size: int, seed: int = 42):
    """Select all judged documents plus fixed-seed distractors for the sole corpus."""
    relevant_ids = {docid for query_gold in gold.values() for docid in query_gold}
    relevant = [row for row in rows if row[0] in relevant_ids]
    distractors = [row for row in rows if row[0] not in relevant_ids]
    random.Random(seed).shuffle(distractors)
    if len(relevant) > size:
        raise ValueError(f"{len(relevant)} relevant docs exceed corpus size {size}")
    return relevant + distractors[: size - len(relevant)]


def evaluate(ranked, gold, qids, k: int = 5):
    """Return macro-averaged standard Recall@K, MRR and NDCG@5."""
    n = len(qids)
    if n == 0:
        raise ValueError("cannot evaluate an empty query split")
    out = {
        "n": n,
        "recall@1": 0.0,
        "recall@5": 0.0,
        "recall@10": 0.0,
        "recall@20": 0.0,
        "mrr": 0.0,
        "ndcg@5": 0.0,
    }
    for qid in qids:
        relevant = gold[qid]
        results = ranked.get(qid, [])
        first = next(
            (rank for rank, docid in enumerate(results, 1) if docid in relevant),
            None,
        )
        if first:
            out["mrr"] += 1.0 / first
        for cutoff in (1, 5, 10, 20):
            found = set(results[:cutoff]) & relevant
            out[f"recall@{cutoff}"] += (
                len(found) / len(relevant) if relevant else 0.0
            )
        dcg = sum(
            1.0 / math.log2(rank + 1)
            for rank, docid in enumerate(results[:k], 1)
            if docid in relevant
        )
        ideal = sum(
            1.0 / math.log2(rank + 1)
            for rank in range(1, min(len(relevant), k) + 1)
        )
        out["ndcg@5"] += dcg / ideal if ideal else 0.0
    for metric in ("recall@1", "recall@5", "recall@10", "recall@20", "mrr", "ndcg@5"):
        out[metric] = round(out[metric] / n, 6)
    return out
