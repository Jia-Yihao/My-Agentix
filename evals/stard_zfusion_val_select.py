"""Dense 候选池 z(原分数)+μ·z(CE) 的 μ 只在 train 留出的 val 300 上定。

第 1 件 A/B 要求「on = z 融合」且「μ 在 val 300 上定」。主融合实验
``stard_ce_fusion.py`` 的 μ=0.5 是在 **blend 排序的候选集**上定的；本脚本
在 **dense 排序的候选集**上重做一次 val-only 网格，得到 A/B 专用的 μ，
dev 一行都不算。CE 分数按 (query, doc) 与候选集无关（已实测 max diff
3.28e-07），因此每题只打一次 dense top-50 的 CE，cand=20 取前缀即可。

产物：docs/evals/stard-zfusion-mu-selection.json（进仓，带 reproduce 与
六字段）；同时把 lab 风格的 CE 缓存键写进 .tmp/stard/ce_zfusion_val.json，
使 ``stard_recall_lab.py --split val --method cez:...`` 可离线复算整个网格。

用法：
  .venv/Scripts/python.exe evals/stard_zfusion_val_select.py \
      --write-summary docs/evals/stard-zfusion-mu-selection.json
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

from evals.perf_stats import floor_rank_quantile  # noqa: E402  (需先补 path)

from evals.stard_eval import POOL, evaluate  # noqa: E402
from evals.stard_recall_lab import DEFAULT_PARAMS, StardLab  # noqa: E402


DATA = ROOT / ".tmp" / "stard"
CE_URL = "http://127.0.0.1:7997/rerank"
CE_MODEL = "text-embedding-bge-reranker-v2-m3"
CAND_MAX = 50
CHARS = 512
CAND_GRID = (20, 50)
MU_GRID = (0.25, 0.5, 1.0, 2.0)
SELECTION_RULE = "max R@5 → max MRR → max NDCG@5 → 更小的 cand → 更小的 μ"

RAW_CACHE = DATA / "ce_zfusion_val_dense.json"
LAB_CACHE = DATA / "ce_zfusion_val.json"


def _lab_key(qid: int, cand: int, fit_n: int) -> str:
    """与 StardLab._ce_scores.key_of 完全一致的缓存键（mu/workers 不入键）。"""
    p = DEFAULT_PARAMS["cez"]
    return (
        f"{qid}|{cand}|{p['chars']}|dense|{p['extra']}|{p['knn_k']}|"
        f"{p['knn_power']}|{p['roc_k']}|{p['roc_beta']}|{p['roc_power']}|"
        f"{p['wd']}|{p['wr']}|{p['wk']}|{p['knn_cand_k']}|{p['knn_cand_power']}|"
        f"{CE_MODEL}|{fit_n}"
    )


def _ce_call(query: str, docs: list[str], id2text: dict[str, str]) -> tuple[list[float], float]:
    payload = json.dumps(
        {
            "model": CE_MODEL,
            "query": query,
            "documents": [id2text[d][:CHARS] for d in docs],
            "top_n": len(docs),
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        CE_URL, data=payload, headers={"Content-Type": "application/json"}
    )
    started = time.perf_counter()
    body: dict | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = json.load(resp)
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1.0)
    elapsed = time.perf_counter() - started
    if not body or "results" not in body:
        raise RuntimeError("CE response missing results")
    scores = [0.0] * len(docs)
    for row in body["results"]:
        i = int(row["index"])
        if 0 <= i < len(docs):
            scores[i] = float(row.get("relevance_score", 0.0))
    return scores, elapsed


def build_val_ce(lab: StardLab, qids: list[int]) -> dict[str, dict[str, Any]]:
    """逐题打一次 dense top-50 的 CE，落盘可断点续跑。"""
    raw: dict[str, dict[str, Any]] = {}
    if RAW_CACHE.exists():
        raw = json.loads(RAW_CACHE.read_text(encoding="utf-8"))
    todo = [q for q in qids if str(q) not in raw]
    print(f"[val-ce] total={len(qids)} todo={len(todo)} cached={len(raw)}", flush=True)
    t0 = time.perf_counter()
    for done, qid in enumerate(todo, 1):
        docs = lab.dense_rank_all[qid][:CAND_MAX]
        scores, elapsed = _ce_call(lab.qs[qid], docs, lab.id2text)
        raw[str(qid)] = {"scores": [round(s, 6) for s in scores], "seconds": round(elapsed, 4)}
        if done % 10 == 0 or done == len(todo):
            DATA.mkdir(parents=True, exist_ok=True)
            RAW_CACHE.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            print(f"  val-ce {done}/{len(todo)} ({time.perf_counter()-t0:.0f}s)", flush=True)
    return raw


def write_lab_cache(raw: dict[str, dict[str, Any]], qids: list[int], fit_n: int) -> None:
    """把 val CE 分数展开成 lab 的 cand=20/50 两种键，供 lab CLI 离线复算。"""
    out: dict[str, dict[str, Any]] = {}
    for qid in qids:
        scores = raw[str(qid)]["scores"]
        seconds = raw[str(qid)]["seconds"]
        for cand in CAND_GRID:
            out[_lab_key(qid, cand, fit_n)] = {
                "scores": scores[:cand],
                "seconds": seconds,
            }
    LAB_CACHE.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")


def run_grid(lab: StardLab, qids: list[int], raw: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [lab.qindex[q] for q in qids]
    dense = lab.S[rows]
    grid: list[dict[str, Any]] = []
    for cand in CAND_GRID:
        ce = {q: np.asarray(raw[str(q)]["scores"][:cand], dtype=np.float32) for q in qids}
        for mu in MU_GRID:
            ranked: dict[int, list[str]] = {}
            for j, qid in enumerate(qids):
                docs = lab.dense_rank_all[qid][:POOL]
                head = docs[:cand]
                cols = [lab.id2i[d] for d in head]
                b = np.asarray(dense[j, cols], dtype=np.float32)
                c = ce[qid]
                bz = (b - b.mean()) / (b.std() + 1e-9)
                cz = (c - c.mean()) / (c.std() + 1e-9)
                order = np.argsort(-(bz + mu * cz), kind="stable")
                ranked[qid] = [head[i] for i in order] + docs[cand:]
            m = evaluate(ranked, lab.gold, qids, k=5)
            grid.append(
                {
                    "cand": cand,
                    "mu": mu,
                    "recall@5": m["recall@5"],
                    "mrr": m["mrr"],
                    "ndcg@5": m["ndcg@5"],
                    "recall@10": m["recall@10"],
                }
            )
    return grid


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write-summary", type=Path)
    args = ap.parse_args()

    lab = StardLab()
    qids, fit = lab.split_qids("val", 0)
    raw = build_val_ce(lab, qids)
    if len(raw) < len(qids):
        raise SystemExit(f"val CE cache incomplete: {len(raw)}/{len(qids)}")
    write_lab_cache(raw, qids, len(fit))
    grid = run_grid(lab, qids, raw)
    grid.sort(
        key=lambda r: (
            -r["recall@5"], -r["mrr"], -r["ndcg@5"], r["cand"], r["mu"]
        )
    )
    best = grid[0]
    mu = float(best["mu"])
    cand = int(best["cand"])
    seconds = sorted(float(raw[str(q)]["seconds"]) for q in qids)

    report: dict[str, Any] = {
        "schema": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "reproduce": " ".join(
            [sys.executable, "evals/stard_zfusion_val_select.py",
             "--write-summary", "docs/evals/stard-zfusion-mu-selection.json"]
        ),
        "config_fingerprint": {
            "profile": "dense-candidate z(dense_score) + mu*z(CE); mu selected on val 300 only",
            "hybrid": False,
            "sparse": False,
            "dense_rerank": False,
            "query_memory": False,
            "embedding_model": "text-embedding-bge-m3",
            "rerank_model": CE_MODEL,
            "candidate_source": "dense_rank_all (与第 1 件 A/B 的 ce: 名次覆盖同候选集)",
            "cand_max_upstream_call": CAND_MAX,
            "chars": CHARS,
            "selected_cand": cand,
            "selected_mu": mu,
        },
        "corpus": {
            "name": "STARD 受控子集 8,000 条（全部金标法条 + seed=42 干扰文档）",
            "rows": 8000,
            "full_corpus_rows": 55348,
            "seed": 42,
            "boundary": "子集只用于同一子集内的方法间比较，绝对值不可与全库 55,348 互读",
        },
        "split": {
            "selection_split": "val (train 留出 300，seed=42)",
            "val_n": len(qids),
            "val_fit_n": len(fit),
            "dev_used_for_selection": False,
            "blind_split_not_touched": "dev 308",
        },
        "metric_definition": {
            "judgement": "精确 docid（qrels），命中定义 = 至少一条相关法条进入 top-k",
            "implementation": "evals/stard_eval.py::evaluate",
            "pool": POOL,
            "search_k": 5,
        },
        "selection_rule": SELECTION_RULE,
        "grid": grid,
        "selected": {"cand": cand, "mu": mu, "val_metrics": {
            "recall@5": best["recall@5"], "mrr": best["mrr"],
            "ndcg@5": best["ndcg@5"], "recall@10": best["recall@10"],
        }},
        "ce_upstream": {
            "n": len(seconds),
            "p50_s": round(floor_rank_quantile(seconds, 0.50), 4),
            "p95_s": round(floor_rank_quantile(seconds, 0.95), 4),
            "max_s": round(seconds[-1], 4),
            "note": "离线逐题上游 /rerank wall time，workers=1；不是在线 P95",
        },
        "note": (
            "μ 只在 val 300 上定，dev 未参与；dense top-50 一次 CE 调用覆盖 "
            "cand=20/50 两个前缀（CE 分数与候选集无关，已实测 max diff 3.28e-07）。"
            "本生成物是第 1 件 A/B 里 z 融合形态的参数来源，不是对外质量口径。"
        ),
    }
    print(json.dumps(report["selected"], ensure_ascii=False, indent=2), flush=True)
    print(f"[val-select] mu={mu} cand={cand} grid={len(grid)}", flush=True)
    if args.write_summary:
        args.write_summary.parent.mkdir(parents=True, exist_ok=True)
        args.write_summary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"[saved] {args.write_summary}", flush=True)


if __name__ == "__main__":
    main()
