"""Phase 31 评估二期验收（分层 golden + 声称一致性 + 敏感性/隐私 + Best@k）。

全部离线、零外部服务；对应 ``docs/migration/Phase28-31_优化规划.md`` §4 Phase 31 DoD：

* 旧 golden（无新字段）全量可跑，**既有 summary 键与基线快照逐位等价**；
  新键只落在 ``difficulty_*`` / ``privacy_*`` / ``best_at_k*`` / ``claim_consistency`` /
  ``sensitivity_*`` 前缀内（全部只增、不进门禁）；
* 声称一致性两例（说对做对 / 说对做错）检出正确；无 trajectory 诚实 skip；
* ``--sensitivity`` 缺省关 = 字节等价；变体只包指令不改问题本体；
* PII 纯离线正则：命中样本单列且从 applicable 分母排除，原文零留存；
* ``best_at_k`` 在 repeat≥2 出数；repeat=1 标注 n/a；
* ``test_ci_gate_contract`` 门键断言不修改、原样通过（在全量批中验证）。

2026-10-26：法律金标集（``evals/legal_golden.jsonl``）随知识图谱/RAG 评测下线。本文件的
「只增不改」断言需要一个**固定输入**才能钉住旧键取值，因此改为 20 行内联冻结金标
:func:`_frozen_golden`——输入写死在测试里，快照值随之重钉。被保护的东西没变（旧键值漂移
仍会红），变的只是它的规模：数据集不再是一个测试依赖。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVALS_DIR = PROJECT_ROOT / "evals"


def _load_run_eval():
    """以文件路径加载 ``evals/run_eval.py``（同时演练 claim_check 双路径导入的
    ``evals.claim_check`` 回落分支）。"""
    spec = importlib.util.spec_from_file_location("p31_run_eval", EVALS_DIR / "run_eval.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["p31_run_eval"] = module
    spec.loader.exec_module(module)
    return module


run_eval = _load_run_eval()
from evals import claim_check  # noqa: E402  —— 包路径加载（pythonpath=["."]）


# ---------------------------------------------------------------------------
# 改动前基线快照（对 :func:`_frozen_golden` × 空预测的 evaluate 输出中**旧键**的取值）。
# 锁「只增不改」：这些旧键的值必须逐位等价，新键只能以前缀白名单出现。
# ---------------------------------------------------------------------------

_PRE_PHASE31_EMPTY: Dict[str, Any] = {
    "citation_correctness": 0.75,
    "citation_correctness_applicable": 0.0,
    "citation_correctness_expected_count": 5,
    "citation_expected_count": 6,
    "citation_free_hit_count": 14,
    "citation_hit_rate": 0.7,
    "citation_hit_rate_applicable": 0.0,
    "golden_route_distribution": {
        "additional-query": 3,
        "file-query": 2,
        "general-query": 4,
        "graphrag-query": 3,
        "image-query": 2,
        "kb-query": 4,
        "text2sql-query": 2,
    },
    "judge_count": 0,
    "judge_enabled": False,
    "judge_mean": None,
    "judge_model": None,
    "mean_task_success": None,
    "missing_prediction_count": 20,
    "multi_agent_meta_count": 0,
    "must_contain": 0.7,
    "must_contain_applicable": 0.0,
    "must_contain_expected_count": 6,
    "must_contain_free_hit_count": 14,
    "pass_at_k": None,
    "pass_k": None,
    "pass_k_applicable": False,
    "pass_k_max_runs_observed": 0,
    "pass_k_requested": None,
    "pass_k_tasks": 0,
    "pass_k_tasks_with_full_runs": 0,
    "pass_k_tasks_without_predictions": 20,
    "pass_k_unavailable_reason": "no_predictions_for_any_golden_row（20 条 golden 全部没有对应预测）",
    "reject_accuracy": 0.75,
    "reject_accuracy_applicable": 0.0,
    "reject_expected_count": 5,
    "reject_false_refusal_rate": 0.0,
    "reject_non_reject_count": 15,
    "reject_ok_count": 15,
    "route_accuracy": 0.0,
    "route_ok_count": 0,
    "total": 20,
}

#: 新键允许出现的前缀/全名（DoD：summary 新键全部落在这些类别内）
_NEW_KEY_PREFIXES = ("difficulty_", "privacy_", "best_at_k", "sensitivity_")
_NEW_KEY_EXACT = frozenset({"claim_consistency"})


def _golden_row(
    gid: str,
    question: str = "问题",
    route: str = "kb-query",
    sources: List[str] | None = None,
    reject: bool = False,
    must_contain: List[str] | None = None,
    expected_citations: List[str] | None = None,
    difficulty: Any = None,
) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "id": gid,
        "question": question,
        "route_type": route,
        "expected_sources": sources or [],
        "must_contain": must_contain or [],
        "expect_reject": reject,
    }
    if expected_citations is not None:
        row["expected_citations"] = expected_citations
    if difficulty is not None:
        row["difficulty"] = difficulty
    return row


def _pred(
    gid: str,
    route: str = "kb-query",
    sources: List[Any] | None = None,
    message: str = "ok",
    status: str = "ok",
    **extra: Any,
) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "id": gid,
        "route_type": route,
        "sources": sources or [],
        "message": message,
        "status": status,
    }
    row.update(extra)
    return row


def _frozen_golden() -> List[Dict[str, Any]]:
    """20 行内联冻结金标：7 类路由全到齐，且各分母都非零。

    冻结（写死在代码里、不读文件）是这条守卫的关键——快照只有对着**不变输入**才有意义。
    构成刻意满足：6 条有引用要求 / 14 条空真、6 条声明 must_contain、5 条带
    expected_citations、5 条越界，因此空预测下的四个全量均值都不是 0 或 1，能真正
    钉住分母口径；7 类路由分布不均，能钉住 ``golden_route_distribution`` 的计数。
    """
    return [
        _golden_row("P01", route="general-query"),
        _golden_row("P02", route="general-query", must_contain=["示例"]),
        _golden_row("P03", route="general-query", reject=True),
        _golden_row("P04", route="additional-query"),
        _golden_row("P05", route="additional-query", must_contain=["示例", "条款"]),
        _golden_row(
            "P06",
            sources=["《示例集》第1条"],
            expected_citations=["《示例集》第1条"],
            must_contain=["示例"],
        ),
        _golden_row(
            "P07",
            sources=["《示例集》第2条", "《示例集》第3条"],
            expected_citations=["《示例集》第2条"],
        ),
        _golden_row("P08", reject=True),
        _golden_row(
            "P09", route="graphrag-query", sources=["《示例集》第4条"], expected_citations=["《示例集》第4条"]
        ),
        _golden_row("P10", route="graphrag-query"),
        _golden_row("P11", route="image-query", sources=["《示例图》第1页"]),
        _golden_row("P12", route="image-query", reject=True),
        _golden_row(
            "P13", route="file-query", sources=["报告.csv"], expected_citations=["报告.csv"], must_contain=["报告"]
        ),
        _golden_row("P14", route="file-query"),
        _golden_row("P15", route="text2sql-query", must_contain=["共"]),
        _golden_row("P16", route="text2sql-query"),
        _golden_row("P17", route="general-query", reject=True),
        _golden_row("P18", sources=["《示例集》第5条"], expected_citations=["《示例集》第5条"]),
        _golden_row("P19", route="additional-query", must_contain=["补充"]),
        _golden_row("P20", route="graphrag-query", reject=True),
    ]


def _frozen_empty_summary() -> Dict[str, Any]:
    """冻结金标 × 空预测的 summary（本文件三条快照断言的共同输入）。"""
    return run_eval.evaluate(_frozen_golden(), [])["summary"]


# ---------------------------------------------------------------------------
# 1. 旧 golden 字节等价（DoD：既有键与基线快照逐位等价；新键前缀白名单）
# ---------------------------------------------------------------------------


class TestPreExistingKeysByteEqual:
    def test_frozen_golden_empty_predictions_matches_snapshot(self):
        summary = _frozen_empty_summary()
        for key, expected in _PRE_PHASE31_EMPTY.items():
            assert summary.get(key) == expected, f"旧键 {key} 值漂移"

    def test_new_keys_confined_to_phase31_prefixes(self):
        summary = _frozen_empty_summary()
        new_keys = set(summary) - set(_PRE_PHASE31_EMPTY)
        assert new_keys, "Phase 31 新键缺席——白名单测试空转"
        illegal = sorted(
            key
            for key in new_keys
            if key not in _NEW_KEY_EXACT
            and not key.startswith(_NEW_KEY_PREFIXES)
        )
        assert not illegal, f"出现白名单外的新 summary 键：{illegal}"

    def test_old_golden_rows_need_no_new_fields(self):
        """冻结金标刻意只用 Phase 31 之前的字段——旧行不做任何补写也能跑通。

        原来这条读的是真实金标文件（断言其中无 ``difficulty`` / ``source`` 键）。输入
        下线后，被保护的性质改由**输入的定义**承载：这里的行没有新字段，而上面两条断言
        成立即证明「缺新字段 ⇒ 只出旧语义、新键诚实缺席或默认」，不会因为字段缺失而
        抛错或造出假样本。
        """
        rows = _frozen_golden()
        assert all("difficulty" not in row for row in rows)
        assert all("source" not in row for row in rows)
        assert len(rows) == len({row["id"] for row in rows}) == 20

    def test_csv_fields_unchanged(self):
        """CSV 列契约不动（privacy/claim 等新字段只进 JSON rows，不进 CSV）。"""
        assert "must_contain_score" in run_eval.CSV_FIELDS
        assert "privacy_flagged" not in run_eval.CSV_FIELDS
        assert "claim_status" not in run_eval.CSV_FIELDS


# ---------------------------------------------------------------------------
# 2. 难度/来源分层（31.1 + by_difficulty 汇总）
# ---------------------------------------------------------------------------


class TestDifficultyLayering:
    def test_missing_difficulty_defaults_to_unknown_group(self):
        golden = [_golden_row("U1"), _golden_row("U2")]
        preds = [_pred("U1"), _pred("U2", route="graphrag-query")]
        summary = run_eval.evaluate(golden, preds)["summary"]
        assert summary["difficulty_unknown_count"] == 2
        assert summary["difficulty_unknown_route_accuracy"] == pytest.approx(0.5)

    def test_groups_split_by_difficulty(self):
        golden = [
            _golden_row("E1", difficulty="easy"),
            _golden_row("H1", difficulty="hard"),
            _golden_row("U1"),
        ]
        preds = [_pred("E1"), _pred("H1", route="graphrag-query"), _pred("U1")]
        summary = run_eval.evaluate(golden, preds)["summary"]
        assert summary["difficulty_easy_count"] == 1
        assert summary["difficulty_easy_route_accuracy"] == pytest.approx(1.0)
        assert summary["difficulty_hard_count"] == 1
        assert summary["difficulty_hard_route_accuracy"] == pytest.approx(0.0)
        assert summary["difficulty_unknown_count"] == 1
        assert summary["difficulty_unknown_route_accuracy"] == pytest.approx(1.0)

    def test_difficulty_group_key_is_sanitized(self):
        golden = [_golden_row("X1", difficulty="Easy!")]
        summary = run_eval.evaluate(golden, [_pred("X1")])["summary"]
        assert summary["difficulty_easy_count"] == 1

    def test_applicable_pools_respected_inside_group(self):
        golden = [
            _golden_row("E1", sources=["《民法典》第577条"], difficulty="easy"),
            _golden_row("E2", difficulty="easy"),
        ]
        preds = [
            _pred("E1", sources=[]),
            _pred("E2"),
        ]
        summary = run_eval.evaluate(golden, preds)["summary"]
        assert summary["difficulty_easy_citation_expected_count"] == 1
        assert summary["difficulty_easy_citation_hit_rate_applicable"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 3. 声称一致性（31.3，Ch7 §7.9「说对做错」）
# ---------------------------------------------------------------------------


class TestClaimCheck:
    def test_extract_claims_normalizes_chinese_numerals_and_dedups(self):
        claims = claim_check.extract_claims(
            "依据《民法典》第五百七十七条，另见《民法典》第577条。"
        )
        assert len(claims) == 1
        assert claims[0].law == "民法典"
        assert claims[0].article == 577

    def test_extract_claims_skips_unparseable(self):
        assert claim_check.extract_claims("普通回答，无法条引用。") == []
        # 只有书名号没有条号 → 不构成法条声称
        assert claim_check.extract_claims("参见《民法典》。") == []

    def test_check_row_ok_when_says_and_does(self):
        """说对做对：答案声称与实际 sources 一致 → ok。"""
        status = claim_check.check_row(
            "根据《民法典》第577条应当承担违约责任。",
            [{"law": "民法典", "article": "第577条", "type": "graphrag"}],
            [],
        )
        assert status == "ok"

    def test_check_row_mismatch_when_says_right_does_wrong(self):
        """说对做错：答案声称《刑法》第264条，轨迹里却只有《民法典》第148条 → mismatch。"""
        status = claim_check.check_row(
            "依据《刑法》第264条构成盗窃罪。",
            [{"law": "民法典", "article": "第148条", "type": "graphrag"}],
            [],
        )
        assert status == "mismatch"

    def test_check_row_no_claims(self):
        assert claim_check.check_row("你好，我能帮你做什么？", [], []) == "no_claims"

    def test_golden_expected_backs_claim(self):
        """证据池并入人工核定真值：sources 未召回但 golden 期望命中 → ok
        （检索覆盖不足归 citation_hit_rate 管，不算虚假声称）。"""
        status = claim_check.check_row(
            "根据《民法典》第577条应承担责任。",
            [],
            ["《民法典》第577条"],
        )
        assert status == "ok"

    def test_law_only_source_supports_claim(self):
        """KB 整本文档来源（无条号）：法规名命中即支持。"""
        status = claim_check.check_row(
            "根据《民法典》第577条应承担责任。",
            [{"type": "kb", "document_id": "d1", "source": "《民法典》"}],
            [],
        )
        assert status == "ok"

    def test_evaluate_reports_mismatch_ids(self):
        golden = [_golden_row("K1"), _golden_row("K2")]
        preds = [
            _pred(
                "K1",
                sources=[{"law": "民法典", "article": "第577条"}],
                message="根据《民法典》第577条应承担责任。",
            ),
            _pred(
                "K2",
                sources=[{"law": "民法典", "article": "第148条"}],
                message="依据《刑法》第264条构成盗窃罪。",
            ),
        ]
        report = run_eval.evaluate(golden, preds)["summary"]["claim_consistency"]
        assert report["applicable"] is True
        assert report["checked"] == 2
        assert report["ok_count"] == 1
        assert report["mismatch_count"] == 1
        assert report["mismatch_ids"] == ["K2"]

    def test_no_trajectory_honest_skip(self):
        summary = run_eval.evaluate([_golden_row("K1")], [])["summary"]
        report = summary["claim_consistency"]
        assert report["applicable"] is False
        assert report["reason"] == "no_predictions_for_any_golden_row"
        assert report["mismatch_count"] == 0

    def test_report_note_requires_human_review(self):
        """Gotcha：条号正则可能误伤——报告口径必须自带人工复核说明。"""
        report = run_eval.evaluate([_golden_row("K1")], [])["summary"]["claim_consistency"]
        assert "人工复核" in report["note"]
        assert "只标记不判分" in report["note"]

    def test_missing_prediction_status_not_counted_as_claims(self):
        summary = run_eval.evaluate([_golden_row("K1")], [])["summary"]
        report = summary["claim_consistency"]
        assert report["missing_prediction_count"] == 1
        assert report["no_claims_count"] == 0


# ---------------------------------------------------------------------------
# 4. Privacy-aware（31.2，Ch7 §7.17）：PII 离线检测 + applicable 分母排除
# ---------------------------------------------------------------------------


class TestPrivacyAware:
    def test_detect_pii_patterns_hit_and_miss(self):
        assert "phone" in run_eval.detect_pii("联系电话13800138000，欢迎咨询")
        assert "national_id" in run_eval.detect_pii("身份证号 110101199001011234")
        assert "bank_card" in run_eval.detect_pii("卡号 6222021234567890123")
        # 法律文本噪声不误伤（条号/案号短数字串）
        assert run_eval.detect_pii("依据《民法典》第577条与第14791条处理") == []
        assert run_eval.detect_pii("（2024）京01民终123456号") == []
        assert run_eval.detect_pii("") == []
        assert run_eval.detect_pii(None) == []

    def test_pii_original_never_leaks_into_report(self):
        golden = [_golden_row("P1", sources=["《民法典》第577条"])]
        preds = [
            _pred(
                "P1",
                sources=[],
                message="请拨打 13800138000 咨询，另见《民法典》第577条。",
            )
        ]
        result = run_eval.evaluate(golden, preds)
        dumped = json.dumps(result["summary"], ensure_ascii=False) + json.dumps(
            result["rows"], ensure_ascii=False
        )
        assert "13800138000" not in dumped, "PII 原文绝不进 summary/rows"
        row = result["rows"][0]
        assert row["privacy_flagged"] is True
        assert row["privacy_kinds"] == ["phone"]

    def test_flagged_sample_excluded_from_citation_applicable(self):
        golden = [
            _golden_row("P1", sources=["《民法典》第577条"]),
            _golden_row("C1", sources=["《民法典》第148条"]),
        ]
        preds = [
            # PII + 引用未命中：若不排除，applicable 会被拉到 0.5
            _pred("P1", sources=[], message="咨询 13800138000。"),
            _pred("C1", sources=["《民法典》第148条"], message="ok"),
        ]
        summary = run_eval.evaluate(golden, preds)["summary"]
        assert summary["privacy_flagged_count"] == 1
        assert summary["privacy_flagged_ids"] == ["P1"]
        assert summary["privacy_applicable_excluded"] == 1
        # 全量口径仍含 flagged（语义不变）
        assert summary["citation_hit_rate"] == pytest.approx(0.5)
        # applicable 分母排除 flagged：只剩 C1 的命中
        assert summary["citation_expected_count"] == 1
        assert summary["citation_hit_rate_applicable"] == pytest.approx(1.0)

    def test_flagged_reject_sample_excluded_from_reject_applicable(self):
        golden = [
            _golden_row("R1", route="general-query", reject=True),
            _golden_row("R2", route="general-query", reject=True),
        ]
        preds = [
            # 越界 + PII，且未拒答（错）——排除后不应拉低 applicable
            _pred("R1", route="general-query", message="实质回答 13800138000。"),
            _pred("R2", route="general-query", message="抱歉，无法回答。", status="rejected"),
        ]
        summary = run_eval.evaluate(golden, preds)["summary"]
        assert summary["privacy_flagged_count"] == 1
        assert summary["reject_expected_count"] == 1
        assert summary["reject_accuracy_applicable"] == pytest.approx(1.0)
        assert summary["reject_accuracy"] == pytest.approx(0.5)

    def test_zero_flag_runs_keep_old_values(self):
        golden = [_golden_row("C1", sources=["《民法典》第577条"])]
        preds = [_pred("C1", sources=["《民法典》第577条"], message="ok")]
        summary = run_eval.evaluate(golden, preds)["summary"]
        assert summary["privacy_flagged_count"] == 0
        assert summary["privacy_flagged_ids"] == []
        assert summary["privacy_applicable_excluded"] == 0
        assert summary["citation_expected_count"] == 1
        assert summary["citation_hit_rate_applicable"] == pytest.approx(1.0)

    def test_applicable_counts_sum_invariant_when_clean(self):
        """0 命中时旧不变量保持：expected + free == total。"""
        summary = _frozen_empty_summary()
        assert (
            summary["citation_expected_count"] + summary["citation_free_hit_count"]
            == summary["total"]
        )
        assert (
            summary["must_contain_expected_count"]
            + summary["must_contain_free_hit_count"]
            == summary["total"]
        )


# ---------------------------------------------------------------------------
# 5. Best@k（31.2，Ch7 §7.14）
# ---------------------------------------------------------------------------


class TestBestAtK:
    def _two_run_case(self):
        golden = [_golden_row("A1", sources=["《民法典》第577条"])]
        preds = [
            # run 0：全错
            _pred("A1", route="text2sql-query", sources=[]),
            # run 1：全对
            _pred("A1", sources=["《民法典》第577条"]),
        ]
        return golden, preds

    def test_repeat_ge2_reports_best_of_runs(self):
        golden, preds = self._two_run_case()
        summary = run_eval.evaluate(golden, preds)["summary"]
        assert summary["best_at_k_applicable"] is True
        assert summary["best_at_k_reason"] is None
        assert summary["best_at_k_tasks_with_multiple_runs"] == 1
        # rubric 分项取 k 次 max：run1 全对 ⇒ 各分项 best = 1.0
        for component, value in summary["best_at_k"].items():
            assert value == pytest.approx(1.0), component
        # 与既有可靠性指标并存且语义不同（pass^k 需全部成功）
        assert summary["pass_k"] == pytest.approx(0.0)
        assert summary["pass_at_k"] == pytest.approx(1.0)

    def test_single_run_marks_na(self):
        golden, preds = self._two_run_case()
        summary = run_eval.evaluate(golden, preds[:1])["summary"]
        assert summary["best_at_k_applicable"] is False
        assert "n_a_single_run" in (summary["best_at_k_reason"] or "")
        assert all(value is None for value in summary["best_at_k"].values())
        assert summary["best_at_k_tasks"] == 1
        assert summary["best_at_k_tasks_with_multiple_runs"] == 0

    def test_no_predictions_reports_no_data(self):
        summary = run_eval.evaluate([_golden_row("A1")], [])["summary"]
        assert summary["best_at_k_applicable"] is False
        assert summary["best_at_k_reason"] == "no_predictions_for_any_golden_row"
        assert summary["best_at_k_tasks"] == 0

    def test_components_are_exactly_five_rubric_metrics(self):
        assert set(summary_keys := run_eval.BEST_AT_K_COMPONENTS) == {
            "route_accuracy",
            "citation_hit_rate",
            "citation_correctness",
            "must_contain",
            "reject_accuracy",
        }
        assert len(summary_keys) == 5


# ---------------------------------------------------------------------------
# 6. Prompt Sensitivity（31.2，Ch7 §7.5）
# ---------------------------------------------------------------------------


class TestSensitivity:
    def test_variants_preserve_question_body(self):
        question = "合同解除的法定情形有哪些？"
        must = ["解除"]  # golden 侧断言关键词：包装不得改写问题本体
        for name, _wrapper in run_eval.SENSITIVITY_VARIANTS:
            wrapped = run_eval.wrap_sensitivity_variant(question, name)
            assert question in wrapped, f"变体 {name} 改写了问题本体"
            for keyword in must:
                if keyword in question:
                    assert keyword in wrapped

    def test_original_variant_is_identity(self):
        question = "原句"
        assert run_eval.wrap_sensitivity_variant(question, "original") == question

    def test_unknown_variant_raises_not_silent(self):
        with pytest.raises(KeyError):
            run_eval.wrap_sensitivity_variant("问题", "not_a_variant")

    def test_ranges_computed_across_variants(self):
        golden = [
            _golden_row("S1", sources=["《民法典》第577条"]),
            _golden_row("S2", route="general-query"),
        ]
        rows = [
            _pred("S1", sources=["《民法典》第577条"], sensitivity_variant="original"),
            _pred("S2", route="general-query", sensitivity_variant="original"),
            _pred("S1", route="text2sql-query", sources=[], sensitivity_variant="prefix_formal"),
            _pred("S2", route="general-query", sensitivity_variant="prefix_formal"),
        ]
        ranges = run_eval.compute_sensitivity_ranges(golden, rows)
        assert ranges["sensitivity_enabled"] is True
        assert ranges["sensitivity_variant_count"] == 2
        assert ranges["sensitivity_variants"] == ["original", "prefix_formal"]
        # original 全对（1.0），perturbed 错 S1（0.5）→ 极差 0.5
        assert ranges["sensitivity_range_route_accuracy"] == pytest.approx(0.5)
        assert set(run_eval.SENSITIVITY_METRICS) <= set(
            key.removeprefix("sensitivity_range_") for key in ranges if key.startswith("sensitivity_range_")
        )

    def test_no_variant_rows_returns_empty_dict(self):
        golden = [_golden_row("S1")]
        rows = [_pred("S1")]
        assert run_eval.compute_sensitivity_ranges(golden, rows) == {}

    def test_parser_default_off_and_flag_on(self):
        parser = run_eval.build_parser()
        assert parser.parse_args([]).sensitivity is False
        assert parser.parse_args(["--sensitivity"]).sensitivity is True

    def test_offline_with_sensitivity_flag_rejected_loudly(self, capsys):
        exit_code = run_eval.main(["--sensitivity", "--offline"])
        assert exit_code == 2
        captured = capsys.readouterr()
        assert "提示敏感性" in captured.err
        assert "离线" in captured.err

    def test_live_mode_accepts_flag_and_fails_on_missing_golden(self, tmp_path, capsys):
        """在线模式放行 --sensitivity（缺文件路径诚实失败，不触发离线拒绝分支）。"""
        exit_code = run_eval.main(
            [
                "--live",
                "--sensitivity",
                "--golden",
                str(tmp_path / "no_golden.jsonl"),
                "--predictions",
                str(tmp_path / "preds.jsonl"),
            ]
        )
        assert exit_code == 2
        captured = capsys.readouterr()
        assert "提示敏感性" not in captured.err
        assert "未找到评估集文件" in captured.err

    def test_offline_variant_data_auto_computes_ranges(self, tmp_path):
        """已录制变体数据离线打分自动附 sensitivity_range_*；无变体 → 无键。"""
        golden_path = tmp_path / "golden.jsonl"
        preds_path = tmp_path / "preds.jsonl"
        results_dir = tmp_path / "results"
        golden_path.write_text(
            json.dumps(_golden_row("S1"), ensure_ascii=False) + "\n", encoding="utf-8"
        )
        preds_path.write_text(
            "\n".join(
                json.dumps(row, ensure_ascii=False)
                for row in [
                    _pred("S1", sensitivity_variant="original"),
                    _pred("S1", route="graphrag-query", sensitivity_variant="polite_opening"),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        assert (
            run_eval.run_offline(
                golden_path=golden_path,
                predictions_path=preds_path,
                results_dir=results_dir,
                timestamp="20260928_000001",
            )
            == 0
        )
        summary = json.loads(
            (results_dir / "20260928_000001.summary.json").read_text(encoding="utf-8")
        )["summary"]
        assert summary["sensitivity_enabled"] is True
        assert summary["sensitivity_range_route_accuracy"] == pytest.approx(1.0)

        # 无变体数据 → 不产生任何 sensitivity_* 键
        preds_path.write_text(
            json.dumps(_pred("S1"), ensure_ascii=False) + "\n", encoding="utf-8"
        )
        assert (
            run_eval.run_offline(
                golden_path=golden_path,
                predictions_path=preds_path,
                results_dir=results_dir,
                timestamp="20260928_000002",
            )
            == 0
        )
        summary2 = json.loads(
            (results_dir / "20260928_000002.summary.json").read_text(encoding="utf-8")
        )["summary"]
        assert not [key for key in summary2 if key.startswith("sensitivity_")]


# ---------------------------------------------------------------------------
# 7. 回流来源（31.4）
# ---------------------------------------------------------------------------


def test_feedback_candidate_marks_source_feedback():
    from evals.feedback_ingest import build_candidate

    candidate = build_candidate(
        {"question": "q", "status": "error", "trace_id": "t1"},
        candidate_id="C001",
        kind="status_failure",
        reason="status=error",
    )
    assert candidate["source"] == "feedback"


# ---------------------------------------------------------------------------
# 8. 门键在场性（test_ci_gate_contract 本身不修改、在全量批中原样通过）
# ---------------------------------------------------------------------------


def test_gate_metrics_still_present_in_summary():
    summary = _frozen_empty_summary()
    for metric in ("citation_hit_rate_applicable", "reject_accuracy_applicable"):
        assert metric in summary
    # 门禁比对用的既有键一个不少
    for key in _PRE_PHASE31_EMPTY:
        assert key in summary
