"""Ablation 聚合：把各变体的 run_eval summary 汇成一张 Agent Benchmark 表。

输入：``<root>/<variant_id>/*.summary.json``（run_eval.py 落盘结构）。
输出：结构化 dict（:func:`aggregate`）+ Markdown 表（:func:`render_markdown`，
即 ChatGPT 评估建议 §六 的 Benchmark Table 形态）。

离线可测/可跑：只有存在 summary 文件的变体才进表，缺失指标显示 ``—``；
零 summary 时返回空表而不是报错（首次跑数前的合法状态）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = ["aggregate", "render_markdown", "load_variants"]

HERE = Path(__file__).resolve().parent

#: summary 字段 → 表列（顺序即列序；缺失 → "—"）
#:
#: 口径一致性：Retrieval / Citation / Keyword / Reject 四列**全部使用
#: ``*_applicable`` 变体**，即只在「真有该项要求」的样本上求均值。
#: 旧写法里 Reject 用的是全量分母 ``reject_accuracy``，而 golden 中
#: ``expect_reject=True`` 的仅占少数，其余条目「不误拒即算对」，
#: 会把该列抬到极高地板、失去区分度（同 citation 的空真问题，见 run_eval 注释）。
#: 迁移期旧 summary 缺 ``*_applicable`` 键时自动显示「—」，不会误读成 0。
_METRIC_COLUMNS = [
    ("pass_k", "Task Success", lambda v: f"{v * 100:.1f}"),
    ("route_accuracy", "Routing", lambda v: f"{v * 100:.1f}"),
    ("citation_hit_rate_applicable", "Retrieval", lambda v: f"{v * 100:.1f}"),
    ("citation_correctness_applicable", "Citation", lambda v: f"{v * 100:.1f}"),
    ("must_contain_applicable", "Keyword", lambda v: f"{v * 100:.1f}"),
    ("reject_accuracy_applicable", "Reject", lambda v: f"{v * 100:.1f}"),
]


def load_variants(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """读变体定义（唯一事实源 variants.json）。"""
    variants_path = path or (HERE / "variants.json")
    with variants_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data["variants"]


def _latest_summary(variant_dir: Path) -> Optional[Dict[str, Any]]:
    """取变体目录下最新一份 summary.json（按文件名时间戳排序）。"""
    if not variant_dir.is_dir():
        return None
    files = sorted(variant_dir.glob("*.summary.json"))
    if not files:
        return None
    with files[-1].open("r", encoding="utf-8") as handle:
        return json.load(handle)


def aggregate(root: Path, *, variants_path: Optional[Path] = None) -> Dict[str, Any]:
    """聚合所有变体 → {rows: [...], missing: [...]}。"""
    root = Path(root)
    rows: List[Dict[str, Any]] = []
    missing: List[str] = []

    for variant in load_variants(variants_path):
        vid = variant["id"]
        summary = _latest_summary(root / vid)
        if summary is None:
            missing.append(vid)
            continue
        row: Dict[str, Any] = {
            "id": vid,
            "label": variant.get("label", vid),
            "total": summary.get("total"),
        }
        for key, _col, _fmt in _METRIC_COLUMNS:
            value = summary.get(key)
            row[key] = round(float(value), 4) if isinstance(value, (int, float)) else None
        # Phase 19：supervisor 指标（信息增量 / 六失败模式）。扁平变体
        # meta_count=0，渲染时跳过；键缺席（旧 summary）→ None，显示 —。
        row["multi_agent_meta_count"] = summary.get("multi_agent_meta_count")
        row["multi_agent_ok_rate"] = summary.get("multi_agent_ok_rate")
        row["multi_agent_rounds_mean"] = summary.get("multi_agent_rounds_mean")
        row["multi_agent_incremental_ratio_mean"] = summary.get(
            "multi_agent_incremental_ratio_mean"
        )
        row["multi_agent_failure_modes"] = summary.get("multi_agent_failure_modes")
        rows.append(row)

    return {"rows": rows, "missing": missing, "root": str(root)}


def render_markdown(aggregated: Dict[str, Any]) -> str:
    """渲染 Benchmark Table（Markdown）。缺数据变体不进表、列缺值显示 —。"""
    header = (
        "| System | Task Success | Routing | Retrieval | Citation | Keyword | Reject | Samples |"
    )
    sep = "|---|---:|---:|---:|---:|---:|---:|---:|"
    lines = [header, sep]
    for row in aggregated.get("rows", []):
        cells = [row.get("label", row.get("id", "?"))]
        for key, _col, fmt in _METRIC_COLUMNS:
            value = row.get(key)
            cells.append("—" if value is None else fmt(value))
        total = row.get("total")
        cells.append(str(total) if total is not None else "—")
        lines.append("| " + " | ".join(cells) + " |")

    missing = aggregated.get("missing") or []
    if missing:
        lines.append("")
        lines.append(f"> 未跑变体（占位，跑完自动进表）：{', '.join(missing)}")

    # Phase 19：supervisor（multi_agent）运行指标附录——只有真跑过 supervisor
    # 拓扑的变体（meta_count>0）才出现；六失败模式计数 + 平均轮次 + 信息增量。
    ma_rows = [
        row
        for row in aggregated.get("rows", [])
        if isinstance(row.get("multi_agent_meta_count"), int)
        and row["multi_agent_meta_count"] > 0
    ]
    if ma_rows:
        lines.append("")
        lines.append("> Multi-Agent 运行指标（Phase 19 · supervisor 拓扑）：")
        for row in ma_rows:
            failure_modes = row.get("multi_agent_failure_modes") or {}
            modes_text = (
                ", ".join(f"{k}={v}" for k, v in sorted(failure_modes.items()))
                or "—"
            )
            rounds = row.get("multi_agent_rounds_mean")
            incr = row.get("multi_agent_incremental_ratio_mean")
            ok_rate = row.get("multi_agent_ok_rate")
            lines.append(
                f"> - **{row.get('label', row.get('id'))}**："
                f"rounds={'—' if rounds is None else rounds} · "
                f"信息增量={'—' if incr is None else incr} · "
                f"ok={'—' if ok_rate is None else f'{ok_rate * 100:.1f}%'} · "
                f"失败模式: {modes_text}"
                f"（n={row['multi_agent_meta_count']}）"
            )

    if not aggregated.get("rows"):
        lines.insert(0, "_(尚无任何变体的评估结果——先运行 run_ablation.py)_")
        lines.append("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    import sys

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else (HERE / "results")
    print(render_markdown(aggregate(target)))
