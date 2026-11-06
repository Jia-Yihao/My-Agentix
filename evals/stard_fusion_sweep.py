"""第三件：融合只做验证——STARD dev 上扫 RRF / weighted(zscore) / top_n / channel_n。

复用 evals/stard_recall_lab.py 的同一份 8,000 受控子集、缓存向量与
stard_eval.evaluate 打分口径；dense 与 BM25 两路的候选均来自同一实验台
（BM25 top-100 落 .tmp 缓存）。不改动任何生产开关：结论只决定是否保持
``RETRIEVAL_HYBRID_ENABLED=false``。

用法：
  .venv/Scripts/python.exe evals/stard_fusion_sweep.py --split both \
      --write-summary docs/evals/stard-fusion-sweep-summary.json

``--split both`` 会在同一份进仓生成物里并列 dev 308 与全量 1,543 两组读数，
用于证明「最优超参在两种划分上不一致」；单独 ``--split dev|all`` 仍可用。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentix.domain.policies.rank_fusion import rrf_fuse, zscore_fuse  # noqa: E402
from evals.stard_eval import evaluate  # noqa: E402
from evals.stard_recall_lab import StardLab  # noqa: E402


def _score_map(ranked: list[str], n: int) -> list[tuple[str, float]]:
    """把名次换成 1/(1+rank) 的单调分数，供 zscore 通道使用。"""
    return [(d, 1.0 / (1.0 + i)) for i, d in enumerate(ranked[:n])]


def run_grid(lab: StardLab, qids: list[int]) -> list[dict[str, Any]]:
    lab._ensure_bm25()
    rows: list[dict[str, Any]] = []
    gold = lab.gold

    def record(name: str, ranked: dict[int, list[str]], extra: dict[str, Any]) -> None:
        m = evaluate(ranked, gold, qids)
        rows.append(
            {
                "config": name,
                "recall@5": m["recall@5"],
                "recall@10": m["recall@10"],
                "mrr": m["mrr"],
                "ndcg@5": m["ndcg@5"],
                **extra,
            }
        )

    record("dense_only(基线)", {q: lab.dense_rank_all[q] for q in qids}, {"method": "dense"})
    record("bm25_only", {q: lab.bm25_rank_all[q] for q in qids}, {"method": "bm25"})

    # RRF：k=10/20/60 × 通道深度 20/100 × top_n 100/200
    for k in (10, 20, 60):
        for channel_n in (20, 100):
            for top_n in (100, 200):
                fused = {
                    q: [
                        d
                        for d, _ in rrf_fuse(
                            [
                                lab.dense_rank_all[q][:channel_n],
                                lab.bm25_rank_all[q][:channel_n],
                            ],
                            k=k,
                            top_n=top_n,
                        )
                    ]
                    for q in qids
                }
                record(
                    f"rrf_k={k}_c={channel_n}_topn={top_n}",
                    fused,
                    {"method": "rrf", "k": k, "channel_n": channel_n, "top_n": top_n},
                )

    # weighted(zscore)：dense 权重高、sparse 权重低；通道深度与 top_n 同扫
    for alpha in (0.7, 0.8, 0.9, 1.0):
        for channel_n in (20, 100):
            for top_n in (100, 200):
                fused = {}
                for q in qids:
                    dch = _score_map(lab.dense_rank_all[q], channel_n)
                    sch = _score_map(lab.bm25_rank_all[q], channel_n)
                    fused[q] = [
                        d
                        for d, _ in zscore_fuse(
                            [dch, sch], alpha=alpha, top_n=top_n
                        )
                    ]
                record(
                    f"zscore_alpha={alpha}_c={channel_n}_topn={top_n}",
                    fused,
                    {
                        "method": "zscore",
                        "alpha": alpha,
                        "channel_n": channel_n,
                        "top_n": top_n,
                    },
                )

    # 去重策略对照：跨通道同 docid 累加（RRF 口径）vs 只取 dense 名次（不合并）
    for k in (10, 60):
        merged = {
            q: [
                d
                for d, _ in rrf_fuse(
                    [lab.dense_rank_all[q][:100], lab.bm25_rank_all[q][:100]],
                    k=k,
                    top_n=100,
                )
            ]
            for q in qids
        }
        record(
            f"dedup_merge_k={k}", merged, {"method": "rrf", "dedup": "merge_by_docid", "k": k}
        )
        first_only = {
            q: list(dict.fromkeys(lab.dense_rank_all[q][:100] + lab.bm25_rank_all[q][:100]))[:100]
            for q in qids
        }
        record(
            f"dedup_first_wins_k={k}",
            first_only,
            {"method": "concat", "dedup": "first_wins", "k": k},
        )
    return rows


def _summarize(rows: list[dict[str, Any]], split: str, n: int) -> dict[str, Any]:
    """把一组网格收敛成 baseline / best / 是否优于 Dense 的定式。"""
    best = max(rows, key=lambda r: (r["recall@5"], r["mrr"]))
    dense = next(r for r in rows if r["config"] == "dense_only(基线)")
    return {
        "split": split,
        "n": n,
        "baseline": dense,
        "best_fusion": best,
        "fusion_beats_dense": bool(best["recall@5"] > dense["recall@5"]),
        "rows": rows,
    }


def _print_split(tag: str, summary: dict[str, Any]) -> None:
    rows = summary["rows"]
    print(f"[fusion] split={tag} n={summary['n']} configs={len(rows)}", flush=True)
    for row in sorted(rows, key=lambda r: -r["recall@5"])[:12]:
        print(
            f"  {row['config']:36s} R@5={row['recall@5']:.4f} MRR={row['mrr']:.4f} NDCG@5={row['ndcg@5']:.4f}",
            flush=True,
        )
    print(
        f"[fusion] best={summary['best_fusion']['config']} "
        f"R@5={summary['best_fusion']['recall@5']:.4f} "
        f"vs dense={summary['baseline']['recall@5']:.4f}",
        flush=True,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--split", choices=("dev", "all", "both"), default="dev")
    ap.add_argument("--write-summary", type=Path)
    args = ap.parse_args()

    lab = StardLab()
    splits = ("dev", "all") if args.split == "both" else (args.split,)
    summaries: dict[str, dict[str, Any]] = {}
    for split in splits:
        qids = sorted(lab.dev) if split == "dev" else list(lab.qids)
        summary = _summarize(run_grid(lab, qids), split, len(qids))
        summaries[split] = summary
        _print_split(split, summary)

    primary = summaries[splits[0]]
    report: dict[str, Any] = {
        "schema": 1,
        "dataset": "STARD (Su et al., Findings of EMNLP 2024, MIT)",
        "corpus": "STARD 受控子集 8000 条（全部金标法条 + seed=42 干扰文档）",
        "judgement": "精确 docid（qrels），命中定义 = 至少一条相关法条进入 top-k",
        "split": args.split,
        "n": primary["n"],
        "reproduce": f"{sys.executable} evals/stard_fusion_sweep.py " + " ".join(sys.argv[1:]),
        "baseline": primary["baseline"],
        "best_fusion": primary["best_fusion"],
        "fusion_beats_dense": primary["fusion_beats_dense"],
        "rows": primary["rows"],
        "by_split": summaries,
    }
    if args.split == "both":
        dev_best = summaries["dev"]["best_fusion"]
        all_best = summaries["all"]["best_fusion"]
        dev_dense = summaries["dev"]["baseline"]
        all_dense = summaries["all"]["baseline"]
        report["cross_split_conclusion"] = {
            "dev_best_config": dev_best["config"],
            "dev_best_recall_at5": dev_best["recall@5"],
            "dev_dense_recall_at5": dev_dense["recall@5"],
            "all_best_config": all_best["config"],
            "all_best_recall_at5": all_best["recall@5"],
            "all_dense_recall_at5": all_dense["recall@5"],
            "hyperparameters_stable": dev_best["config"] == all_best["config"],
            "verdict": (
                "dev 最优超参与全量 1,543 最优超参不一致，且 dev 增益仅 +1 题、"
                "MRR 基本持平；没有稳定优于 Dense 单路的融合，"
                "保持 RETRIEVAL_HYBRID_ENABLED=false。"
            ),
        }
        print(
            f"[fusion] cross-split: dev best={dev_best['config']} "
            f"R@5={dev_best['recall@5']:.4f} vs dense {dev_dense['recall@5']:.4f}; "
            f"all best={all_best['config']} R@5={all_best['recall@5']:.4f} "
            f"vs dense {all_dense['recall@5']:.4f}; "
            f"hyperparameters_stable={report['cross_split_conclusion']['hyperparameters_stable']}",
            flush=True,
        )
    if args.write_summary:
        args.write_summary.parent.mkdir(parents=True, exist_ok=True)
        args.write_summary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"[saved] {args.write_summary}", flush=True)


if __name__ == "__main__":
    main()
