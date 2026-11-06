"""把 minimal-slice 的发布数字**生成**进仓汇总（docs/evals/minimal-slice-summary.json），而不是手抄（工程约定 7 同一口径）。

两种用途，两条互不相同的校验边：
1. 有本地产物时：``--eval <json> --corpus <txt> --write-summary <json>``
   —— 从逐题明细**独立重算**全部指标，写进进仓的汇总 JSON，并顺便对拍
   eval 文件自带的 ``summary`` 块（两条独立计算必须一致，否则退出 1）；
2. 在 CI / 无产物时：``--summary <json> --check README.md``
   —— 只比对「进仓汇总 JSON ↔ README」，不依赖被 gitignore 的 data/demo/。

口径（与进仓汇总逐条对应，改这里必须同步重跑生成器）：
- 分位数是**最近邻秩** ``sorted[round(q*(n-1))]``（n=23 时 P95 = 第 22 小）；
  实现只有**一个源**：``evals/perf_stats.nearest_rank_quantile``（2026-10-27 口径统一，
  此前与本文件并存的线性插值等其它尺子已废止 ⇒ 跨该日期的分位数读数不可直接比）；
- Recall 是 **silver** 口径（chunk 正文 + metadata 出现"法规名 + 第X条"即命中），
  严格口径（金标文档本身进 top-5）另算一列；两者必须同时报；
- 路由残差 = 端到端 − 检索 − 生成（逐题算，再取分位）；
- 覆盖缺口由**语料**推导（金标条文不在语料里），不写死名单。

用法示例：
    .venv/Scripts/python.exe scripts/demo/report_eval.py \
        --eval data/demo/eval_e2e_venv_py312_openai1109.json \
        --alt-eval data/demo/eval_e2e_anaconda_py313_openai147.json \
        --corpus data/kb/legal_corpus.txt \
        --write-summary docs/evals/minimal-slice-summary.json
    .venv/Scripts/python.exe scripts/demo/report_eval.py \
        --summary docs/evals/minimal-slice-summary.json --check README.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[2]
# 直跑脚本时 sys.path[0] 是 scripts/demo/，仓库根不在搜索路径里；分位数的**唯一实现**
# 住在 `evals/perf_stats.py`，先把根插进来才 import 得到（与 local_kb_demo 同法）。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.perf_stats import nearest_rank_quantile  # noqa: E402 - 依赖上面的 ROOT 注入

SUMMARY_SCHEMA = 1


def _pct(values: Sequence[float], q: float) -> float:
    """最近邻秩分位——**委托**全仓唯一实现 ``evals/perf_stats.py``（T2 口径统一）。

    本函数只是转发 + 本脚本自己的空样本立场：这里没有"空样本 = 0.0"那种兜底，
    因为把"没跑成"报成"很快"正是发布数字最不能犯的错（本文件口径②）。
    """
    value = nearest_rank_quantile(values, q)
    if value is None:
        raise ValueError(f"空样本没有分位数可报（q={q}）——不返回 0.0 假装很快")
    return value


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else (ROOT / path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(_resolve(path).read_bytes()).hexdigest()


def compute_metrics(
    per_query: List[Dict[str, Any]],
    corpus_text: str | None = None,
    ext_ids: set[str] | None = None,
) -> Dict[str, Any]:
    """从逐题明细重算全部发布数字（不读 eval 自带的 summary 块）。

    ``ext_ids`` 给出「扩样组」的题号集合（来自扩样 golden 文件），用于把 50 题合并
    样本拆成「基线 23 + 扩样 27」两块的命中数——只算命中数，不重算延迟。
    """
    n = len(per_query)
    if not n:
        raise ValueError("per_query 为空——没有可发布的样本")

    retrieval = [r["retrieval_ms"] for r in per_query]
    generation = [r["generation_ms"] for r in per_query]
    total = [r["total_ms"] for r in per_query]
    residual = [t - a - b for t, a, b in zip(total, retrieval, generation)]
    prompt = [r["prompt_tokens"] for r in per_query]
    completion = [r["completion_tokens"] for r in per_query]
    answer_chars = [r["answer_chars"] for r in per_query]
    ranks = [r["rank"] for r in per_query]

    silver_hits = [r for r in per_query if r["recall_at_k"]]
    strict_hits = [
        r for r in per_query if any(g in r["retrieved_sources"] for g in r["expected_sources"])
    ]

    gap_golds: List[str] = []
    gap_rows: List[str] = []
    if corpus_text is not None:
        for r in per_query:
            for gold in r["expected_sources"]:
                if gold not in corpus_text and gold not in gap_golds:
                    gap_golds.append(gold)
                    gap_rows.append(r["id"])

    covered_rows = n - len(gap_rows) if corpus_text is not None else None

    metrics: Dict[str, Any] = {
        "n": n,
        "silver_hits": len(silver_hits),
        "strict_hits": len(strict_hits),
        "citation_only_hits": len(silver_hits) - len(strict_hits),
        "top1": sum(1 for x in ranks if x == 1),
        "mid_2_5": sum(1 for x in ranks if x and 2 <= x <= 5),
        "miss_ids": [r["id"] for r in per_query if not r["recall_at_k"]],
        "recall_at_5_silver": len(silver_hits) / n,
        "recall_at_5_strict": len(strict_hits) / n,
        "mrr": sum(1.0 / r["rank"] if r["rank"] else 0.0 for r in per_query) / n,
        "retrieval_p50": _pct(retrieval, 0.5),
        "retrieval_p95": _pct(retrieval, 0.95),
        "generation_p50": _pct(generation, 0.5),
        "generation_p95": _pct(generation, 0.95),
        "router_residual_p50": _pct(residual, 0.5),
        "router_residual_p95": _pct(residual, 0.95),
        "e2e_p50": _pct(total, 0.5),
        "e2e_p95": _pct(total, 0.95),
        "prompt_mean": statistics.mean(prompt),
        "completion_mean": statistics.mean(completion),
        "total_tokens_mean": statistics.mean([a + b for a, b in zip(prompt, completion)]),
        "answer_chars_mean": statistics.mean(answer_chars),
        "route_agree": sum(1 for r in per_query if r["route"] == "kb-query"),
        "router_residual_share": statistics.mean(residual) / statistics.mean(total),
        "coverage_gap_golds": gap_golds,
        "coverage_gap_rows": gap_rows,
        "covered_rows": covered_rows,
        "strict_ceiling_hits": covered_rows,
    }

    if ext_ids is not None:
        # 扩样组必须整组在样本里：--limit 截断时宁可失败，也不发布"部分扩样组"的命中数。
        present = {r["id"] for r in per_query}
        missing = sorted(ext_ids - present)
        if missing:
            raise ValueError(f"扩样组有 {len(missing)} 题不在本次明细里（--limit 截断了？）：{missing}")
        ext_rows = [r for r in per_query if r["id"] in ext_ids]
        ext_ranks = [r["rank"] for r in ext_rows]
        metrics |= {
            "ext_n": len(ext_rows),
            "ext_silver_hits": sum(1 for r in ext_rows if r["recall_at_k"]),
            "ext_strict_hits": sum(
                1
                for r in ext_rows
                if any(g in r["retrieved_sources"] for g in r["expected_sources"])
            ),
            "ext_top1": sum(1 for x in ext_ranks if x == 1),
            "ext_mid_2_5": sum(1 for x in ext_ranks if x and 2 <= x <= 5),
        }
    return metrics


def published_strings(metrics: Dict[str, Any], kind: str = "e2e") -> List[Tuple[str, str]]:
    """(口径标签, README 中应逐字出现的字符串)。改格式 = 改 README，两处必须一起动。

    ``kind="retrieval"`` 只发检索层质量口径（Recall/严格/MRR/top-1/覆盖缺口/扩样组），
    因为 50 题扩样集只跑了检索段；**不发布延迟分位数**——cache OFF 下同一份 50 题
    单轮 P50 实测 115.4ms 与 47.8ms（详见 README 口径⑥的抖动结论），单轮分位数不可发布。
    """
    n = metrics["n"]
    strict = metrics["strict_hits"]
    covered = metrics["strict_ceiling_hits"]
    if kind == "retrieval":
        strings: List[Tuple[str, str]] = [
            ("样本量", f"{n}（合并 baseline + ext）"),
            (
                "Recall@5（silver）",
                f"**{metrics['recall_at_5_silver']:.4f}"
                f"（{metrics['silver_hits']}/{n}）**",
            ),
            ("严格 Recall@5", f"**{strict}/{n} = {metrics['recall_at_5_strict']:.4f}**"),
            ("MRR", f"{metrics['mrr']:.4f}"),
            ("top-1 命中", f"{metrics['top1']}/{n}"),
            ("漏召回题号", "/".join(metrics["miss_ids"])),
        ]
        if covered is not None:
            strings.append(
                ("覆盖缺口天花板", f"**{covered}/{n} = {covered / n:.4f}**")
            )
        if "ext_n" in metrics:
            strings.append(
                (
                    "扩样组命中",
                    f"{metrics['ext_silver_hits']}/{metrics['ext_n']}"
                    f"（{metrics['ext_top1']} 题 top-1，"
                    f"{metrics['ext_mid_2_5']} 题 2–5 位）",
                )
            )
        return strings

    strings = [
        ("Recall@5（silver）", f"{metrics['recall_at_5_silver']:.4f}"),
        ("MRR", f"{metrics['mrr']:.4f}"),
        (
            "检索 P50 / P95",
            f"{metrics['retrieval_p50']:.1f} / {metrics['retrieval_p95']:.1f}",
        ),
        (
            "生成 P50 / P95",
            f"{metrics['generation_p50']:.1f} / {metrics['generation_p95']:.1f}",
        ),
        (
            "路由残差 P50 / P95",
            f"{metrics['router_residual_p50']:.1f} / {metrics['router_residual_p95']:.1f}",
        ),
        ("端到端 P50 / P95", f"{metrics['e2e_p50']:.1f} / {metrics['e2e_p95']:.1f}"),
        (
            "token 均值",
            f"prompt {metrics['prompt_mean']:.1f} / completion "
            f"{metrics['completion_mean']:.1f} / 合计 {metrics['total_tokens_mean']:.1f}",
        ),
        (
            "严格 Recall@5",
            f"**{strict}/{n} = {metrics['recall_at_5_strict']:.4f}**",
        ),
        ("路由一致率", f"{metrics['route_agree']}/{n}"),
        ("答案字数均值", f"{metrics['answer_chars_mean']:.1f}"),
        ("路由残差占比", f"{round(metrics['router_residual_share'] * 100)}%"),
    ]
    if covered is not None:
        strings.append(
            ("覆盖缺口天花板", f"**{covered}/{n} = {covered / n:.4f}**")
        )
    return strings


def render_table(metrics: Dict[str, Any], kind: str = "e2e") -> str:
    """Markdown 表体（汇总数字的人读呈现；口径段仍由人写，不在这里生成）。"""
    lines = ["| 指标 | 数值 |", "|---|---|"]
    for label, value in published_strings(metrics, kind):
        lines.append(f"| {label} | {value} |")
    return "\n".join(lines)


def _cross_stack(
    primary: Dict[str, Any], alt: Dict[str, Any], per_primary: List[Dict], per_alt: List[Dict]
) -> Dict[str, Any]:
    pa = {r["id"]: r for r in per_primary}
    pb = {r["id"]: r for r in per_alt}
    shared = sorted(set(pa) & set(pb))
    rank_diffs = [i for i in shared if pa[i]["rank"] != pb[i]["rank"]]
    set_diffs = [i for i in shared if pa[i]["retrieved_sources"] != pb[i]["retrieved_sources"]]
    ratio = (
        alt["retrieval_p50"] / primary["retrieval_p50"] if primary["retrieval_p50"] else None
    )
    return {
        "shared_rows": len(shared),
        "rank_diffs": len(rank_diffs),
        "rank_diff_ids": rank_diffs,
        "set_diffs": len(set_diffs),
        "set_diff_ids": set_diffs,
        "retrieval_p50_ratio": round(ratio, 1) if ratio is not None else None,
        "retrieval_p50_ratio_text": f"{round(ratio)} 倍" if ratio is not None else "",
    }


def _load(path: Path) -> Dict[str, Any]:
    return json.loads(_resolve(path).read_text(encoding="utf-8"))


def _build_summary(args: argparse.Namespace) -> Dict[str, Any]:
    payload = _load(Path(args.eval))
    per_query = payload["per_query"]
    corpus_text = _resolve(Path(args.corpus)).read_text(encoding="utf-8") if args.corpus else None
    kind = "retrieval" if getattr(args, "retrieval_only", False) else "e2e"
    ext_ids: set[str] | None = None
    if getattr(args, "ext_golden", None):
        ext_rows = [
            json.loads(line)
            for line in _resolve(Path(args.ext_golden)).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        ext_ids = {row["id"] for row in ext_rows}
    metrics = compute_metrics(per_query, corpus_text, ext_ids)

    # 两条独立计算必须一致：eval 文件自带的 summary 块 vs 本脚本重算。
    mismatches: List[str] = []
    declared = payload.get("summary") or {}
    pairs = [
        ("recall_at_k", "recall_at_5_silver"),
        ("mrr_within_recall_k", "mrr"),
        ("sample_size", "n"),
    ]
    for declared_key, key in pairs:
        if declared_key in declared and abs(float(declared[declared_key]) - float(metrics[key])) > 5e-5:
            mismatches.append(f"{declared_key}={declared[declared_key]} vs 重算 {metrics[key]}")
    lat = declared.get("latency_ms") or {}
    if abs(float(lat.get("retrieval_p50", 0)) - float(metrics["retrieval_p50"])) > 0.05:
        mismatches.append("retrieval_p50 与 eval 自带 summary 不一致")
    if mismatches:
        raise SystemExit("eval 自带 summary 与本脚本重算不一致：" + "; ".join(mismatches))

    summary: Dict[str, Any] = {
        "schema": SUMMARY_SCHEMA,
        "generator": "scripts/demo/report_eval.py",
        "kind": kind,
        "generated_for": getattr(args, "generated_for", None)
        or "minimal-slice 端到端小样本（指针：evals/README §11）",
        "source_eval": str(args.eval),
        "source_eval_sha256": _sha256(Path(args.eval)),
        "corpus_sha256": _sha256(Path(args.corpus)) if args.corpus else None,
        "golden": declared.get("golden"),
        "ext_golden": getattr(args, "ext_golden", None),
        "env": declared.get("env"),
        "metrics": {k: v for k, v in metrics.items() if not isinstance(v, list)} | {
            "coverage_gap_golds": metrics["coverage_gap_golds"],
            "coverage_gap_rows": metrics["coverage_gap_rows"],
            "miss_ids": metrics["miss_ids"],
        },
        "published": [
            {"label": label, "string": text} for label, text in published_strings(metrics, kind)
        ],
    }
    if args.alt_eval:
        alt_payload = _load(Path(args.alt_eval))
        alt_metrics = compute_metrics(alt_payload["per_query"], None)
        summary["cross_stack"] = {
            "source_eval": str(args.alt_eval),
            "source_eval_sha256": _sha256(Path(args.alt_eval)),
            **_cross_stack(metrics, alt_metrics, per_query, alt_payload["per_query"]),
        }
    return summary


def _verify(target: Path, published: Sequence[Tuple[str, str]]) -> int:
    text = _resolve(target).read_text(encoding="utf-8")
    missing = [f"{label}: {value!r}" for label, value in published if value not in text]
    if missing:
        for item in missing:
            print(f"[BAD] {target} 缺 {item}", file=sys.stderr)
        return 1
    print(f"[OK ] {target} 与生成结果的 {len(list(published))} 个字符串一致")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 / 校验 minimal-slice 的进仓汇总数字")
    parser.add_argument("--eval", help="逐题明细 JSON（本地产物，data/demo/ 已 gitignore）")
    parser.add_argument("--alt-eval", help="跨栈对照 JSON（可选）")
    parser.add_argument("--corpus", help="语料文件（用于推导覆盖缺口；可选）")
    parser.add_argument("--write-summary", help="写汇总 JSON 的路径（进仓）")
    parser.add_argument("--summary", help="已进仓的汇总 JSON（--check 用）")
    parser.add_argument("--check", help="校验目标的 markdown 文件（如 README.md）")
    parser.add_argument(
        "--retrieval-only",
        action="store_true",
        help="只发布检索层口径（50 题扩样集没有生成段，别把 0.0 当实测）",
    )
    parser.add_argument(
        "--ext-golden",
        help="扩样组 golden（可选）：按题号把样本拆出「扩样组命中数」一并发布",
    )
    parser.add_argument(
        "--generated-for",
        help="汇总 JSON 的用途标注（默认 minimal-slice 端到端小样本）",
    )
    args = parser.parse_args(argv)

    # 只校验（无本地产物）⇒ 拿进仓的汇总 JSON 比 README。这是 CI / doc-guard 路径。
    if args.check and not args.eval and not args.write_summary:
        if not args.summary:
            parser.error("--check 单独使用时需要 --summary（进仓汇总 JSON）")
        summary = _load(Path(args.summary))
        return _verify(
            Path(args.check),
            [(item["label"], item["string"]) for item in summary["published"]],
        )

    # 生成路径（可顺带校验）
    if not args.eval:
        parser.error("生成汇总需要 --eval")
    summary = _build_summary(args)
    metrics = summary["metrics"]

    print(render_table(metrics, summary["kind"]))
    if summary.get("cross_stack"):
        cs = summary["cross_stack"]
        print(
            f"\n跨栈：逐题位次差异={cs['rank_diffs']} 检索集合差异={cs['set_diffs']} "
            f"（对照 {cs['source_eval']}）；检索 P50 倍数={cs['retrieval_p50_ratio_text']}"
        )
    gap_rows = metrics["coverage_gap_rows"]
    print(
        f"覆盖缺口 {len(gap_rows)} 条：{gap_rows}"
        if gap_rows
        else "覆盖缺口：未提供 --corpus，本轮未推导（天花板字符串随之跳过）"
    )

    if args.write_summary:
        out = _resolve(Path(args.write_summary))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\n已写汇总：{out}")

    if args.check:
        return _verify(
            Path(args.check),
            [(item["label"], item["string"]) for item in summary["published"]],
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())