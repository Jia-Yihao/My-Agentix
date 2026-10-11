"""Ablation 脚手架测试：变体定义合法性 + 聚合/渲染 Markdown 表（离线）。"""

from __future__ import annotations

import json
from pathlib import Path

from evals.ablations.aggregate import aggregate, load_variants, render_markdown


def test_variants_definition_is_valid():
    """7 个变体、id 唯一、逐级叠加（开关单调开启）——建议 §七 A-G 形态。"""
    variants = load_variants()
    assert [v["id"] for v in variants] == [
        "A_llm_only",
        "B_rag",
        "C_rag_router",
        "D_tools",
        "E_multi_agent",
        "F_memory",
        "G_full",
    ]
    # G_full 必须全开（= 生产默认管线）
    full = variants[-1]["flags"]
    assert all(bool(v) for v in full.values())
    # 裸 LLM 必须全关（full_pipeline 除外）
    bare = variants[0]["flags"]
    assert not any(bool(v) for v in bare.values())


def _write_summary(root: Path, variant_id: str, summary: dict) -> None:
    d = root / variant_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "20260922T000000.summary.json").write_text(
        json.dumps(summary, ensure_ascii=False), encoding="utf-8"
    )


def test_aggregate_with_partial_results(tmp_path: Path):
    _write_summary(
        tmp_path,
        "A_llm_only",
        {
            "total": 102,
            "route_accuracy": 0.7,
            "citation_hit_rate_applicable": 0.8,
            "citation_correctness_applicable": 0.75,
            "must_contain_applicable": 0.65,
            "pass_k": 0.71,
            "reject_accuracy": 0.9,
            "reject_accuracy_applicable": 0.6,
        },
    )
    aggregated = aggregate(tmp_path)
    assert [r["id"] for r in aggregated["rows"]] == ["A_llm_only"]
    assert aggregated["missing"] == [
        "B_rag", "C_rag_router", "D_tools", "E_multi_agent", "F_memory", "G_full",
    ]

    md = render_markdown(aggregated)
    assert (
        "| System | Task Success | Routing | Retrieval | Citation | Keyword | Reject | Samples |"
        in md
    )
    assert "| LLM only | 71.0 | 70.0 | 80.0 | 75.0 | 65.0 | 60.0 | 102 |" in md
    assert "未跑变体" in md and "G_full" in md


def test_reject_column_uses_applicable_denominator(tmp_path: Path):
    """Reject 列必须取 *_applicable 口径——全量 reject_accuracy 会被非越界样本抬高。"""
    _write_summary(
        tmp_path,
        "B_rag",
        {
            "total": 102,
            "reject_accuracy": 0.95,            # 全量口径（含 89 条非越界）
            "reject_accuracy_applicable": 0.31,  # 真实口径（仅 13 条越界样本）
        },
    )
    md = render_markdown(aggregate(tmp_path))
    assert "| 31.0 |" in md
    assert "| 95.0 |" not in md, "表内不得再展示被稀释的全量口径"


def test_missing_applicable_key_renders_placeholder_not_zero(tmp_path: Path):
    """迁移期旧 summary 缺 must_contain_applicable ⇒ 显示「—」，不能误读成 0 分。"""
    _write_summary(tmp_path, "C_rag_router", {"total": 102, "route_accuracy": 1.0})
    md = render_markdown(aggregate(tmp_path))
    assert "| 100.0 | — |" in md


def test_aggregate_empty_root_renders_placeholder(tmp_path: Path):
    aggregated = aggregate(tmp_path)
    assert aggregated["rows"] == []
    md = render_markdown(aggregated)
    assert "尚无任何变体的评估结果" in md
    # 表骨架仍然完整（首跑前可 review 格式）
    assert (
        "| System | Task Success | Routing | Retrieval | Citation | Keyword | Reject | Samples |"
        in md
    )
