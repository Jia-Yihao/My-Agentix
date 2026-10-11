"""R2-3：引用正确性评分 + golden 100+ 扩充测试。

依据：《技术变更与架构演进指南 v2.0》§2.3.7①（黄金集 100+）与 §2.3.7②
（引用正确性评分：期望法条须同时可归因到 sources 且真实出现在答案文本中）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

EVALS_DIR = Path(__file__).resolve().parent.parent / "evals"
GOLDEN_PATH = EVALS_DIR / "legal_golden.jsonl"


def _load_run_eval():
    """以文件路径加载 ``evals/run_eval.py``，避免包导入副作用。"""
    spec = importlib.util.spec_from_file_location("r2_run_eval", EVALS_DIR / "run_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["r2_run_eval"] = module
    return module


run_eval = _load_run_eval()


# ---------------------------------------------------------------- 中文条号解析


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("148", 148),
        ("二十", 20),
        ("十五", 15),
        ("十", 10),
        ("一百", 100),
        ("一百零五", 105),
        ("一百二十", 120),
        ("五百七十七", 577),
        ("一千一百九十一", 1191),
        ("二百零六", 206),
    ],
)
def test_cn_to_int(text, expected):
    assert run_eval._cn_to_int(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "第"])
def test_cn_to_int_fail_open(text):
    """无法解析的形态 fail-open 返回 None（评分按不命中处理，不抛错）。"""
    assert run_eval._cn_to_int(text) is None


def test_cn_to_int_invalid_unit_order_is_tolerant():
    """非法单位组合（十百千）不抛错即可——评分场景下最多算条号不匹配。"""
    result = run_eval._cn_to_int("十百千")
    assert result is None or isinstance(result, int)


# ---------------------------------------------------------------- 答案文本引用抽取


def test_answer_citations_arabic_and_chinese_dedup():
    text = "依据《民法典》第148条……另见《中华人民共和国劳动合同法》第八十二条。"
    cites = run_eval._answer_citations(text)
    assert ("民法典", "148") in cites
    assert ("劳动合同法", "82") in cites  # 中华人民共和国前缀剥离
    assert len(cites) == 2


def test_answer_citations_empty_on_no_match():
    assert run_eval._answer_citations("没有法条引用的普通回答。") == []
    assert run_eval._answer_citations("") == []
    assert run_eval._answer_citations(None) == []


# ---------------------------------------------------------------- 引用正确性评分


def test_citation_correctness_vacuous_truth():
    assert run_eval.score_citation_correctness([], [], "") == 1.0
    assert run_eval.score_citation_correctness(None, None, None) == 1.0


def test_citation_correctness_full_match():
    answer = "根据《民法典》第五百八十五条，违约金过高可请求酌减。"
    score = run_eval.score_citation_correctness(
        ["《民法典》第585条"],
        ["《民法典》第585条 违约金…"],
        answer,
    )
    assert score == 1.0


def test_citation_correctness_source_without_text_is_zero():
    """引用可归因到 sources 但答案文本未真实引用 → 不算正确（防伪造）。"""
    score = run_eval.score_citation_correctness(
        ["《民法典》第584条"],
        ["《民法典》第584条 损失赔偿…"],
        "这个问题的答案是赔偿实际损失。",  # 文本无《》第X条
    )
    assert score == 0.0


def test_citation_correctness_text_without_source_is_zero():
    """答案文本有引用但 sources 不可归因 → 不算正确（检索层缺失）。"""
    score = run_eval.score_citation_correctness(
        ["《民法典》第584条"],
        [],  # 无来源
        "根据《民法典》第584条，应当赔偿。",
    )
    assert score == 0.0


def test_citation_correctness_forged_article_number():
    """条号不符（引用了错误的条文）→ 不算正确。"""
    score = run_eval.score_citation_correctness(
        ["《民法典》第584条"],
        ["《民法典》第584条 损失赔偿…"],
        "根据《民法典》第585条，应当赔偿。",
    )
    assert score == 0.0


def test_citation_correctness_partial_match():
    score = run_eval.score_citation_correctness(
        ["《民法典》第584条", "《劳动合同法》第87条"],
        ["《民法典》第584条 损失…"],
        "根据《民法典》第584条……另参见《劳动合同法》第八十七条。",
    )
    assert score == pytest.approx(0.5)


# ---------------------------------------------------------------- evaluate 集成


def test_evaluate_reports_citation_correctness_fields():
    golden = [
        {"id": "G1", "question": "q1", "route_type": "kb-query", "expected_sources": ["《民法典》第584条"], "expected_citations": ["《民法典》第584条"]},
        {"id": "G2", "question": "q2", "route_type": "general-query"},
    ]
    predictions = [
        {
            "id": "G1",
            "route_type": "kb-query",
            "status": "ok",
            "message": "根据《民法典》第584条，应当赔偿。",
            "sources": ["《民法典》第584条 损失赔偿范围…"],
        },
        {"id": "G2", "route_type": "general-query", "status": "ok", "message": "你好", "sources": []},
    ]

    result = run_eval.evaluate(golden, predictions)
    summary = result["summary"]

    assert result["rows"][0]["citation_correctness"] == 1.0
    assert result["rows"][0]["citation_correctness_expected"] is True
    assert result["rows"][1]["citation_correctness"] == 1.0  # 空真
    assert result["rows"][1]["citation_correctness_expected"] is False
    assert summary["citation_correctness_expected_count"] == 1
    assert summary["citation_correctness_applicable"] == 1.0


def test_evaluate_missing_prediction_scores_zero_correctness():
    golden = [
        {"id": "G1", "question": "q1", "route_type": "kb-query", "expected_citations": ["《民法典》第584条"]},
    ]

    result = run_eval.evaluate(golden, [])

    # 缺失预测按全错计：status=missing_prediction，正确性 0（不空真）
    assert result["rows"][0]["citation_correctness"] == 0.0
    assert result["summary"]["citation_correctness_applicable"] == 0.0


# ---------------------------------------------------------------- golden 100+


def test_golden_covers_100_and_all_routes():
    rows = run_eval.load_jsonl(GOLDEN_PATH)
    assert len(rows) >= 100
    seen = {row["route_type"] for row in rows}
    assert seen == set(run_eval.ALLOWED_ROUTE_TYPES)

    # expected_citations ⊆ expected_sources 语义：允许 expected_sources 为空但
    # expected_citations 非空的样例（法规名级），不允许 citations 里的条目完全无意义
    for row in rows:
        for cite in row.get("expected_citations", []):
            assert "《" in cite and "条" in cite

    # R2-2 效力感知样例存在（旧法废止 → 现行替代条文）
    eff_ids = {row["id"] for row in rows if "废止" in row.get("notes", "")}
    assert len(eff_ids) >= 5
