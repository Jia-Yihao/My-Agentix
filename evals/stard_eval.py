"""STARD 法条检索评测（公开数据集，精确 docid 金标）。

数据集：Su et al., Findings of EMNLP 2024, STARD
https://github.com/oneal2000/STARD （MIT）
1,543 条真实非专业用户咨询、2,717 个条款级相关对、55,348 条候选法条；
官方 train(1,235) / dev(308) 划分零重叠。

与仓库内其它评测的口径边界：本集按 qrels 的**精确 docid** 判命中，
不使用 silver 文本匹配；语料是 STARD 自带的 55,348 条法条，不是
data/kb/legal_corpus.txt 的 4,865 条数据条目。两组数字不可互读。

用法：
  python evals/stard_eval.py --fetch-only
  python evals/stard_eval.py --mode lexical
  python evals/stard_eval.py --mode semantic --subset 8000
  python evals/stard_eval.py --mode lexical --write-summary docs/evals/stard-summary.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DATA = ROOT / ".tmp" / "stard"
REPO = "oneal2000/STARD"
RAW = "https://raw.githubusercontent.com/oneal2000/STARD/main/"
FILES = {
    "queries.json": "data/queries.json",
    "qrels.train.tsv": "data/example/qrels.train.tsv",
    "qrels.dev.tsv": "data/example/qrels.dev.tsv",
    "corpus.tsv": "data/example/corpus.tsv",
}
POOL = 100


def fetch() -> None:
    """按需下载原始文件；已存在则跳过。raw 直连不稳时用 GitHub Contents API 兜底。"""
    import base64
    import subprocess

    DATA.mkdir(parents=True, exist_ok=True)
    for name, remote in FILES.items():
        target = DATA / name
        if target.exists() and target.stat().st_size > 0:
            continue
        payload = None
        try:
            req = urllib.request.Request(RAW + remote, headers={"User-Agent": "stard-eval"})
            with urllib.request.urlopen(req, timeout=120) as r:
                payload = r.read()
        except Exception:
            out = subprocess.run(
                ["gh", "api", f"repos/{REPO}/contents/{remote}", "--jq", ".content"],
                capture_output=True,
                check=True,
            )
            payload = base64.b64decode(out.stdout.decode().strip())
        target.write_bytes(payload)
        print(f"[fetch] {name}: {len(payload)} bytes", flush=True)


def load():
    rows = []
    for line in (DATA / "corpus.tsv").read_text(encoding="utf-8").splitlines():
        p = line.split("\t", 2)
        if len(p) < 2 or not p[0].strip().isdigit():
            continue
        body = p[2].replace("\\n", " ") if len(p) > 2 else ""
        title = p[1].strip()
        rows.append((p[0].strip(), title, f"{title} {body}"))
    qs = {
        x["query_id"]: x["问题"]
        for x in json.loads((DATA / "queries.json").read_text(encoding="utf-8"))
    }
    train, dev = {}, {}
    for line in (DATA / "qrels.train.tsv").read_text(encoding="utf-8").splitlines():
        p = line.split("\t")
        if len(p) >= 4:
            train.setdefault(int(p[0]), set()).add(str(int(p[2])))
    for line in (DATA / "qrels.dev.tsv").read_text(encoding="utf-8").splitlines():
        p = line.split("\t")
        if len(p) >= 2:
            dev.setdefault(int(p[0]), set()).add(str(int(p[1])))
    return rows, qs, train, dev


def subset_rows(rows, gold, n: int, seed: int = 42):
    """保留全部金标法条，其余按固定种子抽样；用于语义通道的可控规模 A/B。"""
    import random

    gold_ids = {d for g in gold.values() for d in g}
    keep = [r for r in rows if r[0] in gold_ids]
    rest = [r for r in rows if r[0] not in gold_ids]
    random.Random(seed).shuffle(rest)
    return keep + rest[: max(0, n - len(keep))]


def evaluate(ranked, gold, qids, k: int = 5):
    n = len(qids)
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
        g = gold[qid]
        lst = ranked.get(qid, [])
        first = next((i for i, d in enumerate(lst, 1) if d in g), None)
        if first:
            out["mrr"] += 1.0 / first
            for kk in (1, 5, 10, 20):
                if first <= kk:
                    out[f"recall@{kk}"] += 1
        dcg = sum(1.0 / math.log2(i + 2) for i, d in enumerate(lst[:k]) if d in g)
        ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(g), k)))
        out["ndcg@5"] += dcg / ideal if ideal else 0.0
    for key in ("recall@1", "recall@5", "recall@10", "recall@20", "mrr", "ndcg@5"):
        out[key] = round(out[key] / n, 6)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch-only", action="store_true")
    ap.add_argument("--mode", choices=("lexical", "semantic"), default="lexical")
    ap.add_argument("--subset", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--embedding-url", default="http://127.0.0.1:1234/v1/embeddings")
    ap.add_argument("--model", default="text-embedding-bge-m3")
    ap.add_argument("--write-summary", type=Path)
    args = ap.parse_args()

    fetch()
    if args.fetch_only:
        return
    from agentix.domain.policies.rank_fusion import rrf_fuse
    from agentix.retrieval.infra.rag.sparse import BM25Index

    rows, qs, train, dev = load()
    gold = {**train, **dev}
    if args.subset:
        rows = subset_rows(rows, gold, args.subset)
    qids = sorted(gold)
    if args.limit:
        qids = qids[: args.limit]
    corpus_note = (
        f"STARD 受控子集 {len(rows)} 条（全部金标法条 + seed=42 干扰文档）"
        if args.subset
        else f"STARD 全库 {len(rows)} 条法条"
    )
    print(
        f"[data] {corpus_note}; queries={len(qids)}; train={len(train)} dev={len(dev)}", flush=True
    )

    t0 = time.perf_counter()
    idx = BM25Index([(r[0], r[2], {}) for r in rows])
    sparse = {q: [d for d, _ in idx.search(qs[q], POOL)] for q in qids}
    print(f"[lexical] BM25 top-{POOL} in {time.perf_counter()-t0:.1f}s", flush=True)

    columns: dict[str, dict[int, list[str]]] = {"bm25": sparse}
    if args.mode == "semantic":
        dense = semantic_rank(rows, qs, qids, args)
        columns["dense"] = dense
        columns["rrf"] = {
            q: [d for d, _ in rrf_fuse([sparse[q], dense[q]], k=60, top_n=POOL)] for q in qids
        }

    report = {
        "schema": 1,
        "dataset": "STARD (Su et al., Findings of EMNLP 2024, MIT)",
        "corpus": corpus_note,
        "judgement": "精确 docid（qrels），命中定义 = 至少一条相关法条进入 top-k",
        "pool": POOL,
        "splits": {"train": len(train), "dev": len(dev)},
        "columns": {},
    }
    for name, ranked in columns.items():
        report["columns"][name] = {
            "dev": evaluate(ranked, dev, sorted(dev)),
            "train": evaluate(ranked, train, sorted(train)),
            "all": evaluate(ranked, gold, qids),
        }
        d = report["columns"][name]["dev"]
        print(
            f"[{name:6s}] DEV  R@5={d['recall@5']:.4f} R@10={d['recall@10']:.4f} "
            f"MRR={d['mrr']:.4f} NDCG@5={d['ndcg@5']:.4f}",
            flush=True,
        )

    if args.write_summary:
        args.write_summary.parent.mkdir(parents=True, exist_ok=True)
        args.write_summary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"[saved] {args.write_summary}", flush=True)


def semantic_rank(rows, qs, qids, args):
    """全库语义召回：embedding 落盘缓存（float16），分块矩阵乘取 top-POOL。"""
    cache = DATA / f"emb_{len(rows)}_{args.model.replace('/', '_')}.npz"
    if cache.exists():
        z = np.load(cache)
        D, Q, cached = z["doc"], z["q"], list(z["qids"])
    else:
        D = np.asarray(embed_all([r[2][:800] for r in rows], args), dtype=np.float32)
        D /= np.linalg.norm(D, axis=1, keepdims=True) + 1e-9
        Q = np.asarray(embed_all([qs[q][:400] for q in qids], args), dtype=np.float32)
        Q /= np.linalg.norm(Q, axis=1, keepdims=True) + 1e-9
        np.savez_compressed(
            cache, doc=D.astype(np.float16), q=Q.astype(np.float16), qids=np.asarray(qids)
        )
        cached = qids
    if list(cached) != list(qids):
        raise SystemExit("embedding 缓存的 qid 顺序与本次不一致，删除缓存后重跑")
    ids = [r[0] for r in rows]
    Df = D.astype(np.float32)
    out: dict[int, list[str]] = {}
    t0 = time.perf_counter()
    for s in range(0, len(qids), 32):
        block = Q[s : s + 32].astype(np.float32)
        sims = block @ Df.T
        for j, qid in enumerate(qids[s : s + 32]):
            top = np.argpartition(-sims[j], min(POOL, len(ids) - 1))[:POOL]
            top = top[np.argsort(-sims[j][top], kind="stable")]
            out[qid] = [ids[i] for i in top]
    print(f"[semantic] dense top-{POOL} in {time.perf_counter()-t0:.1f}s", flush=True)
    return out


def embed_all(texts, args, batch: int = 96):
    out = []
    for i in range(0, len(texts), batch):
        chunk = texts[i : i + batch]
        payload = json.dumps({"model": args.model, "input": chunk}).encode("utf-8")
        req = urllib.request.Request(
            args.embedding_url,
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": "Bearer lm-studio"},
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    data = json.load(r)["data"]
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(1.5)
        data.sort(key=lambda x: x["index"])
        out.extend(x["embedding"] for x in data)
        if (i // batch) % 20 == 0:
            print(f"  embed {i+len(chunk)}/{len(texts)}", flush=True)
    return out


if __name__ == "__main__":
    main()
