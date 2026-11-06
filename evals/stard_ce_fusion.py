"""blend + 同候选集 z(CE) 二次融合：val 300 定参 → dev 308 一次盲测。

被 team-lead 指定为「目前最可能摸到 0.80 的路径」。与既有 ``ce:`` 的
**纯 CE 名次覆盖**不同，本脚本只做「同一候选集内 z(blend 原分数) + μ·z(CE)」
融合，绝不用 CE 名次覆盖原排序（该形态已实测有害：dev60 0.7833→0.7667）。

严格盲测纪律（纠正 2）：
1. ``--phase select``：只在 train 留出的 **val 300** 上用缓存向量扫 blend 配置，
   按预先声明的规则（R@5 → MRR → NDCG@5 → 更简单的 wk）定死唯一配置；
2. ``--phase ce``：为 val 300 + dev 308 生成 CE 分数，落盘到
   ``.tmp/stard/ce_fusion_scores.json``（可断点续跑），并在 ``--phase report``
   时把用到的分数**原样写进进仓 JSON**，使结论不依赖上游服务即可复算；
3. ``--phase tune``：只在 **val 300** 上扫 (cand, mu)，定死唯一组合；
4. ``--phase dev``：dev 308 **只跑一次、只报这一组**；不输出任何 dev 变体网格。

CE 分数与候选集大小无关（实测 max|s100[:50]-s50| = 2.31e-07），故每题只调一次
上游、取 blend top-50，后续各 cand 只需取分数前缀；cand 上限 50，不外推到 100。

历史读数如实标注：既有 blend dev 0.7760 是在 dev 上做过变体选择后报的，
生成物 ``prior_blend_reading_caveat`` 明确写「dev 上做过变体选择，非严格盲测」。

用法（Git Bash 绝对路径风格，一次跑完）：

  .venv/Scripts/python.exe evals/stard_ce_fusion.py --phase all \
      --write-summary docs/evals/stard-ce-fusion-summary.json

分步（CE 阶段长，可后台跑，缓存断点续跑）：

  .venv/Scripts/python.exe evals/stard_ce_fusion.py --phase select
  .venv/Scripts/python.exe evals/stard_ce_fusion.py --phase ce
  .venv/Scripts/python.exe evals/stard_ce_fusion.py --phase report \
      --write-summary docs/evals/stard-ce-fusion-summary.json
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

from evals.perf_stats import floor_rank_quantile  # noqa: E402  (需先补 path)

from evals.stard_eval import DATA, POOL, evaluate  # noqa: E402
from evals.stard_recall_lab import StardLab  # noqa: E402


CE_URL = "http://127.0.0.1:7997/rerank"
CE_MODEL = "text-embedding-bge-reranker-v2-m3"
EMBEDDING_MODEL = "text-embedding-bge-m3"
CAND_MAX = 50
CHARS = 512
SEARCH_K = 5

DISK_CACHE = DATA / "ce_fusion_scores.json"
SELECTION = DATA / "ce_fusion_selection.json"

# val-only 配置网格：全部只用缓存向量，不碰 CE、不碰 dev。
BLEND_GRID: list[dict[str, Any]] = [
    {"wd": 0.3, "wr": 1.0, "wk": 0.1, "k": 10, "beta": 1.0, "power": 2.0,
     "knn_k": 20, "knn_power": 4.0},
    {"wd": 0.0, "wr": 1.0, "wk": 0.0, "k": 10, "beta": 1.0, "power": 2.0,
     "knn_k": 20, "knn_power": 4.0},
    {"wd": 0.3, "wr": 1.0, "wk": 0.0, "k": 10, "beta": 1.0, "power": 2.0,
     "knn_k": 20, "knn_power": 4.0},
    {"wd": 0.0, "wr": 1.0, "wk": 0.1, "k": 10, "beta": 1.0, "power": 2.0,
     "knn_k": 20, "knn_power": 4.0},
    {"wd": 0.3, "wr": 1.0, "wk": 0.2, "k": 10, "beta": 1.0, "power": 2.0,
     "knn_k": 20, "knn_power": 4.0},
    {"wd": 0.3, "wr": 1.0, "wk": 0.1, "k": 20, "beta": 1.0, "power": 2.0,
     "knn_k": 20, "knn_power": 4.0},
    {"wd": 0.3, "wr": 1.0, "wk": 0.1, "k": 10, "beta": 0.5, "power": 2.0,
     "knn_k": 20, "knn_power": 4.0},
    {"wd": 0.3, "wr": 1.0, "wk": 0.1, "k": 10, "beta": 1.0, "power": 2.0,
     "knn_k": 20, "knn_power": 2.0},
]

CAND_GRID = (10, 20, 30, 40, 50)
MU_GRID = (0.25, 0.5, 1.0, 2.0)

# 预先声明的定参规则：任何阶段都不得事后按 dev 结果改这个规则。
SELECTION_RULE = (
    "max R@5 → max MRR → max NDCG@5 → 更小的 cand（延迟）→ 更小的 mu → 更小的 wk"
)


def _fp(cfg: dict[str, Any]) -> str:
    canonical = json.dumps(cfg, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(  # nosec B324 - 非安全用途：配置指纹
        canonical.encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:10]


def _brief(m: dict[str, float]) -> dict[str, float]:
    return {
        "recall@5": m["recall@5"],
        "mrr": m["mrr"],
        "ndcg@5": m["ndcg@5"],
        "recall@10": m["recall@10"],
    }


def _sort_key(m: dict[str, float], cand: int, mu: float, wk: float) -> tuple:
    return (-m["recall@5"], -m["mrr"], -m["ndcg@5"], cand, mu, wk)


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


def phase_select(lab: StardLab) -> dict[str, Any]:
    """只在 val 300 上定死唯一 blend 配置；不触碰 dev，不调用 CE。"""
    qids, fit = lab.split_qids("val", 0)
    rows: list[dict[str, Any]] = []
    for cfg in BLEND_GRID:
        scores = lab.blend_scores(qids, fit, cfg)
        ranked = lab._top_from_sims(scores, POOL, qids)
        m = evaluate(ranked, lab.gold, qids, k=5)
        rows.append({"cfg": cfg, "wk": float(cfg["wk"]), "metrics": _brief(m)})
    rows.sort(key=lambda r: _sort_key(r["metrics"], 0, 0.0, r["wk"]))
    winner = rows[0]
    out = {
        "stage": "select",
        "split": "val",
        "n": len(qids),
        "fit_n": len(fit),
        "selection_rule": SELECTION_RULE,
        "rows": rows,
        "selected_blend": winner["cfg"],
        "selected_blend_fp": _fp(winner["cfg"]),
        "selected_val_metrics": winner["metrics"],
        "dev_touched": False,
    }
    SELECTION.parent.mkdir(parents=True, exist_ok=True)
    SELECTION.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[select] val={len(qids)} winner={winner['cfg']}", flush=True)
    print(f"[select] R@5={winner['metrics']['recall@5']:.4f} MRR={winner['metrics']['mrr']:.4f}", flush=True)
    return out


def _read_selection() -> dict[str, Any]:
    if not SELECTION.exists():
        raise SystemExit("run --phase select first")
    return json.loads(SELECTION.read_text(encoding="utf-8"))


def _plan(lab: StardLab, cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """按 split 计算 blend 分数矩阵与 top-CAND_MAX 候选列表（口径唯一真源）。"""
    plan: dict[str, dict[str, Any]] = {}
    for split in ("val", "dev"):
        qids, fit = lab.split_qids(split, 0)
        scores = lab.blend_scores(qids, fit, cfg)
        cand = lab._top_from_sims(scores, CAND_MAX, qids)
        plan[split] = {"qids": qids, "fit": fit, "scores": scores, "cand": cand}
    return plan


def _cache_key(qid: int, split: str, fit_n: int, blend_fp: str) -> str:
    return f"{qid}|{split}|{CAND_MAX}|{CHARS}|{fit_n}|{blend_fp}|{CE_MODEL}"


def phase_ce(lab: StardLab, cfg: dict[str, Any], blend_fp: str) -> dict[str, Any]:
    """生成 val+dev 的 CE 分数，落盘（每 10 题 flush，可断点续跑）。"""
    plan = _plan(lab, cfg)
    cache: dict[str, dict[str, Any]] = {}
    if DISK_CACHE.exists():
        cache = json.loads(DISK_CACHE.read_text(encoding="utf-8"))
    todo: list[tuple[str, int, str, str]] = []
    for split, info in plan.items():
        for qid in info["qids"]:
            key = _cache_key(qid, split, len(info["fit"]), blend_fp)
            if key not in cache:
                todo.append((key, qid, split, info["cand"][qid]))
    print(f"[ce] total={sum(len(i['qids']) for i in plan.values())} todo={len(todo)} cached={len(cache)}", flush=True)
    t0 = time.perf_counter()
    for done, (key, qid, split, docs) in enumerate(todo, 1):
        try:
            scores, elapsed = _ce_call(lab.qs[qid], docs, lab.id2text)
            cache[key] = {
                "scores": [round(float(s), 6) for s in scores],
                "seconds": round(elapsed, 4),
                "split": split,
                "qid": qid,
            }
        except Exception as exc:  # noqa: BLE001 - 单题失败留待下次续跑
            print(f"[ce] FAIL qid={qid} split={split}: {exc}", flush=True)
            continue
        if done % 10 == 0:
            DISK_CACHE.parent.mkdir(parents=True, exist_ok=True)
            DISK_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            print(f"[ce] {done}/{len(todo)} ({time.perf_counter()-t0:.0f}s)", flush=True)
    DISK_CACHE.parent.mkdir(parents=True, exist_ok=True)
    DISK_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    print(f"[ce] done rows={len(cache)} wall={time.perf_counter()-t0:.0f}s", flush=True)
    return {"rows": len(cache), "todo": len(todo)}


def _z_fuse_ranked(
    lab: StardLab,
    info: dict[str, Any],
    split: str,
    cache: dict[str, dict[str, Any]],
    blend_fp: str,
    cand: int,
    mu: float,
) -> dict[int, list[str]]:
    """同一候选集内 z(blend 原分数) + mu * z(CE) 重排，不用 CE 名次覆盖。"""
    qids = info["qids"]
    scores = info["scores"]
    cand_lists = info["cand"]
    fit_n = len(info["fit"])
    out: dict[int, list[str]] = {}
    for j, qid in enumerate(qids):
        docs = cand_lists[qid][:cand]
        ce = cache[_cache_key(qid, split, fit_n, blend_fp)]["scores"][:cand]
        cols = [lab.id2i[d] for d in docs]
        b = np.asarray(scores[j, cols], dtype=np.float32)
        c = np.asarray(ce, dtype=np.float32)
        bz = (b - b.mean()) / (b.std() + 1e-9)
        cz = (c - c.mean()) / (c.std() + 1e-9)
        order = np.argsort(-(bz + mu * cz), kind="stable")
        out[qid] = [docs[i] for i in order] + cand_lists[qid][cand:]
    return out


def phase_report(lab: StardLab, sel: dict[str, Any], write_summary: Path | None) -> dict[str, Any]:
    cfg = sel["selected_blend"]
    blend_fp = sel["selected_blend_fp"]
    plan = _plan(lab, cfg)
    if not DISK_CACHE.exists():
        raise SystemExit("run --phase ce first")
    cache = json.loads(DISK_CACHE.read_text(encoding="utf-8"))

    # 完整性：val 300 + dev 308 每题都必须在盘上有分数，缺一不出数。
    missing = [
        (split, qid)
        for split, info in plan.items()
        for qid in info["qids"]
        if _cache_key(qid, split, len(info["fit"]), blend_fp) not in cache
    ]
    if missing:
        raise SystemExit(f"CE cache incomplete, missing {len(missing)} e.g. {missing[:3]}")

    # val-only 定 (cand, mu)：dev 一行都不算。
    val_info = plan["val"]
    grid: list[dict[str, Any]] = []
    for cand in CAND_GRID:
        for mu in MU_GRID:
            ranked = _z_fuse_ranked(lab, val_info, "val", cache, blend_fp, cand, mu)
            m = evaluate(ranked, lab.gold, val_info["qids"], k=5)
            grid.append({"cand": cand, "mu": mu, "metrics": _brief(m)})
    grid.sort(
        key=lambda r: _sort_key(r["metrics"], r["cand"], r["mu"], float(cfg["wk"]))
    )
    best = grid[0]
    cand, mu = int(best["cand"]), float(best["mu"])

    # dev 308：只跑这一组、只报这一组。
    dev_info = plan["dev"]
    dev_ranked = _z_fuse_ranked(lab, dev_info, "dev", cache, blend_fp, cand, mu)
    dev_metrics = _brief(evaluate(dev_ranked, lab.gold, dev_info["qids"], k=5))
    base_blend = lab._top_from_sims(dev_info["scores"], POOL, dev_info["qids"])
    blend_metrics = _brief(evaluate(base_blend, lab.gold, dev_info["qids"], k=5))
    dense_metrics = _brief(
        evaluate({q: lab.dense_rank_all[q] for q in dev_info["qids"]},
                 lab.gold, dev_info["qids"], k=5)
    )

    # 硬上限归因：最终 dev 结果的未命中题，金标在候选池里的位置分桶。
    buckets = {"6-10": 0, "11-20": 0, "21-50": 0, "51-100": 0, "not_in_top100": 0}
    for qid in dev_info["qids"]:
        g = lab.gold[qid]
        if any(d in g for d in dev_ranked[qid][:SEARCH_K]):
            continue
        pos = next((i for i, d in enumerate(base_blend[qid], 1) if d in g), None)
        if pos is None or pos > 100:
            buckets["not_in_top100"] += 1
        elif pos <= 10:
            buckets["6-10"] += 1
        elif pos <= 20:
            buckets["11-20"] += 1
        elif pos <= 50:
            buckets["21-50"] += 1
        else:
            buckets["51-100"] += 1
    n_dev = len(dev_info["qids"])
    unreachable = buckets["not_in_top100"]

    # 进仓 CE 分数缓存（只保留本次用到的前缀长度，避免生成物过大）
    committed_scores: dict[str, list[float]] = {}
    for split, info in plan.items():
        for qid in info["qids"]:
            key = _cache_key(qid, split, len(info["fit"]), blend_fp)
            committed_scores[key] = cache[key]["scores"][:cand]
    ce_seconds = sorted(
        float(cache[_cache_key(q, s, len(plan[s]["fit"]), blend_fp)]["seconds"])
        for s in plan
        for q in plan[s]["qids"]
    )

    report: dict[str, Any] = {
        "schema": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "reproduce": " ".join([sys.executable, "evals/stard_ce_fusion.py", "--phase all",
                               "--write-summary", "docs/evals/stard-ce-fusion-summary.json"]),
        "config_fingerprint": {
            "profile": "blend + same-candidate z(CE) fusion (offline experiment, not production default)",
            "hybrid": False,
            "sparse": False,
            "dense_rerank": False,
            "query_memory": False,
            "embedding_model": EMBEDDING_MODEL,
            "rerank_model": CE_MODEL,
            "candidate_pool": CAND_MAX,
            "search_k": SEARCH_K,
            "fusion_form": "z(blend_score) + mu * z(ce_score) on the SAME candidate set; CE rank override explicitly NOT used",
            "blend": cfg,
            "blend_fp": blend_fp,
            "selected_cand": cand,
            "selected_mu": mu,
            "cand_max_upstream_call": CAND_MAX,
            "chars": CHARS,
        },
        "corpus": {
            "name": "STARD 受控子集 8,000 条（全部金标法条 + seed=42 干扰文档）",
            "rows": 8000,
            "full_corpus_rows": 55348,
            "seed": 42,
            "embedding_cache": ".tmp/stard/emb_8000_text-embedding-bge-m3.npz",
            "boundary": "子集只用于同一子集内的方法间比较，绝对值不可与全库 55,348 互读",
        },
        "split": {
            "selection_split": "val (train 留出 300，seed=42)",
            "val_n": len(val_info["qids"]),
            "val_fit_n": len(val_info["fit"]),
            "blind_split": "dev 308",
            "dev_n": n_dev,
            "dev_fit_n": len(dev_info["fit"]),
            "dev_runs": 1,
            "note": "dev 只跑 val 定死的唯一配置一次，无 dev 变体网格",
        },
        "metric_definition": {
            "judgement": "精确 docid（qrels），命中定义 = 至少一条相关法条进入 top-k",
            "implementation": "evals/stard_eval.py::evaluate",
            "search_k": SEARCH_K,
            "pool": POOL,
            "ce_latency_note": (
                "ce_upstream_seconds 是离线逐题上游 /rerank wall time（workers=1），"
                "由 CE 缓存离线重算，不是在线真实调用墙钟，不得当线上 P95 对外报"
            ),
        },
        "blind_discipline": {
            "selection_rule": SELECTION_RULE,
            "blend_grid_n": len(BLEND_GRID),
            "fusion_grid_n": len(grid),
            "all_selection_on_val": True,
            "dev_variant_sweep": False,
            "prior_blend_reading_caveat": (
                "既有 blend dev R@5 0.7760 是在 dev 上比较 knn/rocchio/blend/rocchio_knn "
                "四个变体后选出的，dev 上做过变体选择，非严格盲测，带选择性乐观；"
                "本生成物不把 0.7760 包装成盲测数字。"
            ),
            "dev60_08333_caveat": (
                "dev60 R@5 0.8333 是离线扫出来的方向性读数，未在 val 定参、CE 缓存未落盘，"
                "不作为对外口径、不写进 README。"
            ),
        },
        "val_selection": {
            "blend_grid": sel["rows"],
            "selected_blend": cfg,
            "selected_blend_val_metrics": sel["selected_val_metrics"],
            "fusion_grid": grid,
            "selected": {"cand": cand, "mu": mu, "val_metrics": best["metrics"]},
        },
        "dev_blind": {
            "method": "blend + z(CE)",
            "metrics": dev_metrics,
            "baselines": {"dense": dense_metrics, "blend_no_ce": blend_metrics},
            "delta_vs_dense": {
                k: round(dev_metrics[k] - dense_metrics[k], 6) for k in dev_metrics
            },
            "delta_vs_blend": {
                k: round(dev_metrics[k] - blend_metrics[k], 6) for k in dev_metrics
            },
            "per_query": [
                [qid, next((i for i, d in enumerate(dev_ranked[qid], 1)
                            if d in lab.gold[qid]), 0), len(lab.gold[qid])]
                for qid in dev_info["qids"]
            ],
        },
        "ceiling_attribution": {
            "dev_misses": sum(buckets.values()),
            "buckets": buckets,
            "hard_ceiling_recall_at5": round((n_dev - unreachable) / n_dev, 6),
            "unreachable_reason": (
                f"{unreachable} 题金标根本不在 blend top-100 候选池内，"
                "任何同候选集重排（含 CE）都无法救回；这是硬上限，不是调参问题。"
            ),
        },
        "ce_upstream": {
            "n": len(ce_seconds),
            "p50_s": round(floor_rank_quantile(ce_seconds, 0.50), 4),
            "p95_s": round(floor_rank_quantile(ce_seconds, 0.95), 4),
            "max_s": round(ce_seconds[-1], 4),
            "note": "离线逐题上游 /rerank wall time，workers=1；不是在线 P95",
        },
        "ce_score_cache": committed_scores,
        "ce_score_cache_note": (
            "每题 blend top-50 的 CE 分数前缀（取 selected_cand 长度）原样进仓，"
            "守卫可据此离线重算 val 网格与 dev 盲测，不依赖上游服务。"
            "完整 50 维分数与逐题 seconds 在 .tmp/stard/ce_fusion_scores.json（仅本地，gitignore）。"
        ),
    }
    print(json.dumps({
        "selected_blend": cfg,
        "selected_cand": cand, "selected_mu": mu,
        "val_metrics": best["metrics"],
        "dev_blind": dev_metrics,
        "baselines": {"dense": dense_metrics, "blend_no_ce": blend_metrics},
        "ceiling": report["ceiling_attribution"],
        "ce_upstream": report["ce_upstream"],
    }, ensure_ascii=False, indent=2), flush=True)
    if write_summary:
        write_summary.parent.mkdir(parents=True, exist_ok=True)
        write_summary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"[saved] {write_summary}", flush=True)
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--phase", choices=("select", "ce", "report", "all"), default="all")
    ap.add_argument("--write-summary", type=Path)
    args = ap.parse_args()

    lab = StardLab(subset=8000)
    sel: dict[str, Any] = {}
    if args.phase in ("select", "all"):
        sel = phase_select(lab)
    if args.phase in ("ce", "report", "all"):
        if not sel:
            sel = _read_selection()
        cfg, blend_fp = sel["selected_blend"], sel["selected_blend_fp"]
        if args.phase in ("ce", "all"):
            phase_ce(lab, cfg, blend_fp)
        if args.phase in ("report", "all"):
            phase_report(lab, sel, args.write_summary)


if __name__ == "__main__":
    main()
