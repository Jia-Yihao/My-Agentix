"""Govern the single STARD retrieval report and its generated README table."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVALS = ROOT / "docs" / "evals"
README = ROOT / "README.md"
REPORT = EVALS / "stard-default-retrieval.json"

TABLE_START = "<!-- STARD_TABLE_START -->"
TABLE_END = "<!-- STARD_TABLE_END -->"
REQUIRED_FIELDS = (
    "generated_at",
    "config_fingerprint",
    "corpus",
    "split",
    "metric_definition",
    "reproduce",
)


def _load_report() -> dict:
    return json.loads(REPORT.read_text(encoding="utf-8"))


def generate_table() -> str:
    """Render the only public retrieval quality row from the locked dev report."""
    report = _load_report()
    baseline = report["current_project_baseline"]
    metrics = report["locked_dev_comparison"]["baseline"]["metrics"]
    return "\n".join(
        (
            TABLE_START,
            "> 本表由 `evals/stard_summary_governance.py` 从唯一基准报告自动生成；手工改动会被守卫拦下。",
            "",
            "| 数据集 | 默认方案 | 通道候选深度 | dev Recall@1 | dev Recall@5 | dev Recall@10 | dev Recall@20 | dev MRR | dev NDCG@5 |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|",
            (
                f"| STARD 8,000 文档受控子集 | Dense + BM25/z-score (α={baseline['alpha']}) "
                f"| {baseline['channel_depth']} "
                f"| {metrics['recall@1']:.4f} | {metrics['recall@5']:.4f} "
                f"| {metrics['recall@10']:.4f} | {metrics['recall@20']:.4f} "
                f"| {metrics['mrr']:.4f} | {metrics['ndcg@5']:.4f} |"
            ),
            TABLE_END,
            "",
        )
    )


def _replace_table(text: str, table: str) -> str:
    if TABLE_START not in text or TABLE_END not in text:
        raise ValueError("README is missing STARD table markers")
    start = text.index(TABLE_START)
    end = text.index(TABLE_END) + len(TABLE_END)
    if text[end : end + 1] == "\n":
        end += 1
    return text[:start] + table + text[end:]


def check() -> list[str]:
    problems: list[str] = []
    try:
        report = _load_report()
    except (OSError, json.JSONDecodeError) as exc:
        return [f"cannot load the single STARD report: {exc}"]

    missing = [field for field in REQUIRED_FIELDS if field not in report]
    if missing:
        problems.append(f"stard-default-retrieval.json missing fields: {missing}")
    if report.get("corpus", {}).get("documents") != 8000:
        problems.append("the canonical STARD controlled corpus must contain 8,000 documents")
    if report.get("split") != {"train": 1235, "validation": 300, "dev": 308}:
        problems.append("STARD query split must be train=1,235 / validation=300 / dev=308")
    if "fraction" not in report.get("metric_definition", {}).get("recall", "").lower():
        problems.append("Recall@K must be defined as a fraction of all relevant qrels docids")
    summaries = sorted(EVALS.glob("stard-*.json"))
    if [path.name for path in summaries] != [REPORT.name]:
        problems.append(
            "docs/evals must retain only stard-default-retrieval.json as the STARD quality report"
        )

    try:
        readme = README.read_text(encoding="utf-8")
        if TABLE_START not in readme or TABLE_END not in readme:
            problems.append("README is missing the generated STARD table")
        elif _replace_table(readme, generate_table()).strip() != readme.strip():
            problems.append("README STARD table differs from the report; run --write")
        outside = re.sub(
            re.escape(TABLE_START) + r".*?" + re.escape(TABLE_END),
            "",
            readme,
            flags=re.S,
        )
        if re.search(r"\b0\.\d{4}\b", outside):
            problems.append("README contains hand-written retrieval metrics outside the generated table")
    except (OSError, KeyError, TypeError) as exc:
        problems.append(f"cannot validate README table: {exc}")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--write", action="store_true")
    args = parser.parse_args()

    if args.check:
        problems = check()
        if problems:
            print("STARD governance check FAILED:")
            for problem in problems:
                print(f"  - {problem}")
            raise SystemExit(1)
        print("STARD governance check PASSED")
        return

    current = README.read_text(encoding="utf-8")
    updated = _replace_table(current, generate_table())
    README.write_text(updated, encoding="utf-8")
    print("README STARD table updated")


if __name__ == "__main__":
    main()
