"""P2-1 评估集与回归框架的单测（零外部服务，可在 ``-m "not integration"`` 下跑）。

覆盖：
* golden JSONL 可解析、条数 ≥20、7 类路由 + 6 个出口节点全覆盖、≥2 条越界
* 三个确定性打分函数的分支对拍（命中/未命中/零期望/误拒/正确拒答）
* 离线模式端到端（tmp_path 下的临时 golden + predictions → CSV/summary）
* 基线文件缺失时的清晰报错且非静默通过
* ``--judge`` 无端点 → ``NotImplementedError`` 诚实失败（退出码 3）
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVALS_DIR = PROJECT_ROOT / "evals"
GOLDEN_PATH = EVALS_DIR / "legal_golden.jsonl"


def _load_run_eval():
    """以文件路径加载 ``evals/run_eval.py``，避免包导入副作用。"""
    spec = importlib.util.spec_from_file_location("p21_run_eval", EVALS_DIR / "run_eval.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["p21_run_eval"] = module
    spec.loader.exec_module(module)
    return module


run_eval = _load_run_eval()


# ---------------------------------------------------------------------------
# golden 评估集结构
# ---------------------------------------------------------------------------


def _load_golden() -> List[Dict[str, Any]]:
    return run_eval.load_jsonl(GOLDEN_PATH)


def test_golden_jsonl_parses_and_has_enough_rows():
    rows = _load_golden()
    assert len(rows) >= 20, f"golden 条数不足：{len(rows)}"
    assert all(isinstance(row, dict) for row in rows)


def test_golden_ids_unique_and_schema_complete():
    rows = _load_golden()
    ids = [row["id"] for row in rows]
    assert len(ids) == len(set(ids)), "golden id 存在重复"

    required = {
        "id",
        "question",
        "route_type",
        "expected_sources",
        "must_contain",
        "expect_reject",
    }
    for row in rows:
        assert required.issubset(row.keys()), f"{row.get('id')} 缺字段"
        assert isinstance(row["id"], str) and row["id"]
        assert isinstance(row["question"], str) and row["question"]
        assert isinstance(row["route_type"], str)
        assert isinstance(row["expected_sources"], list)
        assert all(isinstance(src, str) for src in row["expected_sources"])
        assert isinstance(row["must_contain"], list)
        assert isinstance(row["expect_reject"], bool)


def test_golden_covers_all_seven_route_types():
    rows = _load_golden()
    seen = {row["route_type"] for row in rows}
    assert seen == set(run_eval.ALLOWED_ROUTE_TYPES), (
        f"路由覆盖不全，缺失：{set(run_eval.ALLOWED_ROUTE_TYPES) - seen}"
    )
    assert len(run_eval.ALLOWED_ROUTE_TYPES) == 7


def test_golden_covers_all_six_exit_nodes():
    rows = _load_golden()
    seen_exits = {run_eval.EXIT_NODE_BY_ROUTE[row["route_type"]] for row in rows}
    assert seen_exits == set(run_eval.EXPECTED_EXIT_NODES), (
        f"出口节点覆盖不全，缺失：{set(run_eval.EXPECTED_EXIT_NODES) - seen_exits}"
    )
    assert len(run_eval.EXPECTED_EXIT_NODES) == 6


def test_golden_has_at_least_two_reject_rows():
    rows = _load_golden()
    rejects = [row for row in rows if row["expect_reject"]]
    assert len(rejects) >= 2, f"越界样本不足：{len(rejects)}"


def test_golden_non_reject_rows_have_valid_route():
    rows = _load_golden()
    for row in rows:
        assert row["route_type"] in run_eval.ALLOWED_ROUTE_TYPES


# ---------------------------------------------------------------------------
# score_route
# ---------------------------------------------------------------------------


def test_score_route_hit_and_miss():
    assert run_eval.score_route("kb-query", "kb-query") is True
    assert run_eval.score_route("kb-query", "  KB-Query ") is True
    assert run_eval.score_route("kb-query", "graphrag-query") is False
    assert run_eval.score_route("kb-query", None) is False
    assert run_eval.score_route(None, "kb-query") is False


# ---------------------------------------------------------------------------
# score_citations
# ---------------------------------------------------------------------------


def test_score_citations_full_hit_with_string_sources():
    hit = run_eval.score_citations(
        ["《民法典》第577条"], ["《民法典》第577条"]
    )
    assert hit == pytest.approx(1.0)


def test_score_citations_hit_with_dict_sources():
    """graphrag 形态：law + article；kb 形态：document_id + source。"""
    graphrag = run_eval.score_citations(
        ["《民法典》第577条"],
        [{"type": "graphrag", "law": "民法典", "article": "第577条", "source": "《民法典》第577条"}],
    )
    assert graphrag == pytest.approx(1.0)

    kb = run_eval.score_citations(
        ["《劳动合同法》第87条"],
        [{"type": "kb", "document_id": "doc-1", "source": "《劳动合同法》第87条"}],
    )
    assert kb == pytest.approx(1.0)


def test_score_citations_miss_wrong_article_or_law():
    assert run_eval.score_citations(
        ["《民法典》第577条"], ["《民法典》第148条"]
    ) == pytest.approx(0.0)
    assert run_eval.score_citations(
        ["《民法典》第577条"], ["《刑法》第264条"]
    ) == pytest.approx(0.0)
    assert run_eval.score_citations(["《民法典》第577条"], []) == pytest.approx(0.0)


def test_score_citations_partial_hit_is_fraction():
    rate = run_eval.score_citations(
        ["《民法典》第577条", "《劳动合同法》第87条"],
        ["《民法典》第577条"],
    )
    assert rate == pytest.approx(0.5)


def test_score_citations_zero_expected_branch():
    """零期望：无引用要求，恒为 1.0（即使实际带了来源）。"""
    assert run_eval.score_citations([], []) == pytest.approx(1.0)
    assert run_eval.score_citations(None, []) == pytest.approx(1.0)
    assert run_eval.score_citations([], ["《民法典》第1条"]) == pytest.approx(1.0)


def test_score_citations_ignores_extra_actual_sources():
    rate = run_eval.score_citations(
        ["《民法典》第577条"],
        ["《民法典》第148条", "《民法典》第577条", "《刑法》第264条"],
    )
    assert rate == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# score_reject
# ---------------------------------------------------------------------------


def test_score_reject_true_when_reject_expected_and_refused():
    assert run_eval.score_reject(True, "rejected", "抱歉，无法回答。", []) is True
    assert run_eval.score_reject(True, "refused", "", []) is True
    assert run_eval.score_reject(True, "ok", "抱歉，这超出了我的服务范围。", []) is True


def test_score_reject_true_when_degraded_without_sources():
    assert run_eval.score_reject(True, "degraded", "", []) is True
    # 降级但带了引用 → 不算正确拒答
    assert run_eval.score_reject(
        True, "degraded", "这是回答", [{"document_id": "d1", "source": "x"}]
    ) is False


def test_score_reject_false_when_reject_expected_but_answered():
    assert run_eval.score_reject(
        True, "ok", "根据《民法典》第577条……", [{"law": "民法典"}]
    ) is False


def test_score_reject_false_positive_detection():
    """非越界问题被误拒 → 记错。"""
    assert run_eval.score_reject(False, "rejected", "抱歉，无法回答。", []) is False
    assert run_eval.score_reject(False, "ok", "抱歉，无法提供该服务。", []) is False


def test_score_reject_non_reject_ok_paths():
    assert run_eval.score_reject(False, "ok", "根据《民法典》第148条，可以撤销。", []) is True
    # 降级但未拒答 → 不算误拒
    assert run_eval.score_reject(False, "degraded", "以下为参考信息……", []) is True


def test_refusal_markers_exclude_disclaimer_words():
    """统一免责声明含“专业律师”，不能因为出现该词就判为拒答。"""
    disclaimer = "本回答由 AI 基于公开法律文本生成，不构成正式法律意见，具体以官方发布文本及专业律师意见为准。"
    assert run_eval.score_reject(False, "ok", disclaimer, []) is True


# ---------------------------------------------------------------------------
# evaluate 汇总
# ---------------------------------------------------------------------------


def _golden_row(gid: str, question: str, route: str, sources=None, reject=False):
    return {
        "id": gid,
        "question": question,
        "route_type": route,
        "expected_sources": sources or [],
        "must_contain": [],
        "expect_reject": reject,
    }


def test_evaluate_summary_metrics():
    golden = [
        _golden_row("A1", "提问一", "kb-query", ["《民法典》第577条"]),
        _golden_row("A2", "提问二", "graphrag-query", ["《刑法》第264条"]),
        _golden_row("A3", "越界问题", "general-query", [], reject=True),
    ]
    predictions = [
        {"id": "A1", "route_type": "kb-query", "sources": ["《民法典》第577条"], "message": "可以", "status": "ok"},
        {"id": "A2", "route_type": "text2sql-query", "sources": [], "message": "错误路由", "status": "ok"},
        {"id": "A3", "route_type": "general-query", "sources": [], "message": "抱歉，无法回答。", "status": "rejected"},
    ]
    result = run_eval.evaluate(golden, predictions)
    summary = result["summary"]

    assert summary["total"] == 3
    assert summary["route_accuracy"] == pytest.approx(2 / 3, abs=1e-4)
    # (1.0 + 0.0 + 1.0) / 3
    assert summary["citation_hit_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert summary["reject_accuracy"] == pytest.approx(1.0)
    assert summary["reject_expected_count"] == 1
    assert summary["missing_prediction_count"] == 0
    assert summary["golden_route_distribution"]["kb-query"] == 1


def test_evaluate_missing_prediction_counted_as_miss():
    golden = [_golden_row("B1", "提问", "kb-query", ["《民法典》第577条"])]
    result = run_eval.evaluate(golden, [])
    summary = result["summary"]
    assert summary["missing_prediction_count"] == 1
    assert summary["route_accuracy"] == pytest.approx(0.0)
    assert summary["citation_hit_rate"] == pytest.approx(0.0)
    assert result["rows"][0]["status"] == "missing_prediction"


def test_evaluate_accepts_live_route_field_name():
    """在线录制之外的兼容路径：预测里用 ``route`` 字段名。"""
    golden = [_golden_row("C1", "提问", "kb-query")]
    predictions = [{"id": "C1", "route": "kb-query", "sources": [], "message": "ok", "status": "ok"}]
    result = run_eval.evaluate(golden, predictions)
    assert result["rows"][0]["route_ok"] is True


# ---------------------------------------------------------------------------
# 离线端到端
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_offline_end_to_end_produces_csv_and_summary(tmp_path, capsys):
    golden_path = tmp_path / "golden.jsonl"
    pred_path = tmp_path / "preds.jsonl"
    results_dir = tmp_path / "results"

    _write_jsonl(
        golden_path,
        [
            _golden_row("T1", "问题一", "kb-query", ["《民法典》第577条"]),
            _golden_row("T2", "问题二", "general-query"),
        ],
    )
    _write_jsonl(
        pred_path,
        [
            {"id": "T1", "route_type": "kb-query", "sources": ["《民法典》第577条"], "message": "可以", "status": "ok"},
            {"id": "T2", "route_type": "general-query", "sources": [], "message": "你好", "status": "ok"},
        ],
    )

    exit_code = run_eval.run_offline(
        golden_path=golden_path,
        predictions_path=pred_path,
        results_dir=results_dir,
        timestamp="20240101_000000",
    )
    assert exit_code == 0

    csv_path = results_dir / "20240101_000000.csv"
    summary_path = results_dir / "20240101_000000.summary.json"
    assert csv_path.exists()
    assert summary_path.exists()

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert list(reader.fieldnames) == list(run_eval.CSV_FIELDS)
        rows = list(reader)
    assert [row["id"] for row in rows] == ["T1", "T2"]
    assert rows[0]["route_ok"] == "True"
    assert float(rows[0]["citation_hit_rate"]) == pytest.approx(1.0)

    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert payload["timestamp"] == "20240101_000000"
    assert payload["summary"]["total"] == 2
    assert payload["summary"]["route_accuracy"] == pytest.approx(1.0)

    captured = capsys.readouterr()
    assert "评估汇总" in captured.out


def test_offline_missing_baseline_fails_loudly(tmp_path, capsys):
    golden_path = tmp_path / "golden.jsonl"
    _write_jsonl(golden_path, [_golden_row("T1", "问题一", "kb-query")])
    missing_pred = tmp_path / "does_not_exist.jsonl"

    exit_code = run_eval.run_offline(
        golden_path=golden_path,
        predictions_path=missing_pred,
        results_dir=tmp_path / "results",
    )
    assert exit_code != 0, "缺失基线必须非零退出，不能静默通过"

    captured = capsys.readouterr()
    assert "--record" in captured.err
    assert "baseline_predictions.jsonl" in captured.err or "基线" in captured.err


def test_offline_missing_golden_fails(tmp_path, capsys):
    exit_code = run_eval.run_offline(
        golden_path=tmp_path / "no_golden.jsonl",
        predictions_path=tmp_path / "no_preds.jsonl",
        results_dir=tmp_path / "results",
    )
    assert exit_code != 0


# ---------------------------------------------------------------------------
# judge 端点预检与 CLI
# ---------------------------------------------------------------------------


def test_judge_without_endpoint_raises_honest_error(monkeypatch):
    """无端点 → NotImplementedError 诚实失败，绝不给假分。

    本地 ``.env`` 可能带真实端点凭据——测试必须显式清空，禁止触网。
    """
    from agentix.config import settings

    monkeypatch.setattr(settings, "LLM_API_KEY", "")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "")
    with pytest.raises(NotImplementedError):
        run_eval.judge_similarity("期望", "实际")


def test_judge_default_disabled_in_parser():
    parser = run_eval.build_parser()
    args = parser.parse_args([])
    assert args.judge is False

    args_on = parser.parse_args(["--judge"])
    assert args_on.judge is True

    args_off = parser.parse_args(["--no-judge"])
    assert args_off.judge is False


def test_cli_help_shows_chinese_and_exits_zero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        run_eval.main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "法律领域评估集" in out
    assert "--offline" in out and "--record" in out


def test_cli_judge_flag_returns_error_code(tmp_path, capsys, monkeypatch):
    """--judge 无端点 → 非零退出并给出中文提示（诚实失败）。"""
    from agentix.config import settings

    monkeypatch.setattr(settings, "LLM_API_KEY", "")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "")
    golden_path = tmp_path / "golden.jsonl"
    pred_path = tmp_path / "preds.jsonl"
    _write_jsonl(golden_path, [_golden_row("T1", "问题一", "kb-query")])
    _write_jsonl(
        pred_path,
        [{"id": "T1", "route_type": "kb-query", "sources": [], "message": "ok", "status": "ok"}],
    )

    exit_code = run_eval.main(
        [
            "--offline",
            "--judge",
            "--golden",
            str(golden_path),
            "--predictions",
            str(pred_path),
            "--results-dir",
            str(tmp_path / "results"),
        ]
    )
    assert exit_code != 0
    captured = capsys.readouterr()
    assert "人工" in captured.err or "校准" in captured.err


def test_real_golden_evaluates_against_synthetic_full_hit_baseline():
    """用 golden 自身构造“全命中”预测，端到端验证三个指标可同时到 1.0。"""
    golden_rows = _load_golden()
    predictions = []
    for row in golden_rows:
        if row["expect_reject"]:
            predictions.append(
                {
                    "id": row["id"],
                    "route_type": row["route_type"],
                    "sources": [],
                    "message": "抱歉，该问题超出我的服务范围，无法回答。",
                    "status": "rejected",
                }
            )
        else:
            predictions.append(
                {
                    "id": row["id"],
                    "route_type": row["route_type"],
                    "sources": list(row["expected_sources"]),
                    "message": "根据相关法条……",
                    "status": "ok",
                }
            )

    result = run_eval.evaluate(golden_rows, predictions)
    summary = result["summary"]
    assert summary["route_accuracy"] == pytest.approx(1.0)
    assert summary["citation_hit_rate"] == pytest.approx(1.0)
    assert summary["reject_accuracy"] == pytest.approx(1.0)
    assert summary["missing_prediction_count"] == 0


# ---------------------------------------------------------------------------
# 批次 7：空真地板可见性（P2-1 补丁）
#
# 背景：score_citations 对 expected_sources 为空的条目返回 1.0（vacuous truth，
# 单条语义合理），聚合层逐条求均值 ⇒ 无引用要求的条目每条都注入一个免费满分。
# 修复方式：**不改变逐条打分**，在 summary 暴露真实分母与适用口径。
# ---------------------------------------------------------------------------


class TestVacuousFloorVisibility:
    def _mixed_golden(self):
        # 2 条有引用要求 + 2 条无引用要求（空真）
        return [
            _golden_row("C1", "提问一", "kb-query", ["《民法典》第577条"]),
            _golden_row("C2", "提问二", "graphrag-query", ["《刑法》第264条"]),
            _golden_row("C3", "闲聊", "general-query", []),
            _golden_row("C4", "计数", "text2sql-query", []),
        ]

    def test_summary_exposes_citation_denominators(self):
        predictions = [
            {"id": "C1", "route_type": "kb-query", "sources": ["《民法典》第577条"], "message": "ok", "status": "ok"},
            {"id": "C2", "route_type": "graphrag-query", "sources": ["《刑法》第264条"], "message": "ok", "status": "ok"},
            {"id": "C3", "route_type": "general-query", "sources": [], "message": "你好", "status": "ok"},
            {"id": "C4", "route_type": "text2sql-query", "sources": [], "message": "共 3 件", "status": "ok"},
        ]
        summary = run_eval.evaluate(self._mixed_golden(), predictions)["summary"]

        assert summary["citation_expected_count"] == 2
        assert summary["citation_free_hit_count"] == 2
        assert summary["reject_non_reject_count"] == 4
        # 有引用要求的 2 条全部命中 ⇒ 适用口径满分
        assert summary["citation_hit_rate_applicable"] == pytest.approx(1.0)
        # 全量均值被空真抬高，但两个口径都可用
        assert summary["citation_hit_rate"] == pytest.approx(1.0)

    def test_applicable_mean_not_inflated_by_free_hits(self):
        predictions = [
            {"id": "C1", "route_type": "kb-query", "sources": [], "message": "没引到", "status": "ok"},
            {"id": "C2", "route_type": "graphrag-query", "sources": [], "message": "没引到", "status": "ok"},
            {"id": "C3", "route_type": "general-query", "sources": [], "message": "你好", "status": "ok"},
            {"id": "C4", "route_type": "text2sql-query", "sources": [], "message": "共 3 件", "status": "ok"},
        ]
        summary = run_eval.evaluate(self._mixed_golden(), predictions)["summary"]

        # 全量口径：空真 2 条贡献 2.0，均值 = 2/4
        assert summary["citation_hit_rate"] == pytest.approx(0.5, abs=1e-4)
        # 适用口径：0 命中 / 2 条 —— 空真不再掩盖真实失败
        assert summary["citation_expected_count"] == 2
        assert summary["citation_hit_rate_applicable"] == pytest.approx(0.0)

    def test_zero_applicable_items_report_zero_not_one(self):
        """分母为 0 时适用口径必须给 0.0——绝不能制造新的空真。"""
        golden = [
            _golden_row("D1", "闲聊", "general-query", []),
            _golden_row("D2", "计数", "text2sql-query", []),
        ]
        predictions = [
            {"id": "D1", "route_type": "general-query", "sources": [], "message": "你好", "status": "ok"},
            {"id": "D2", "route_type": "text2sql-query", "sources": [], "message": "共 3 件", "status": "ok"},
        ]
        summary = run_eval.evaluate(golden, predictions)["summary"]

        assert summary["citation_expected_count"] == 0
        assert summary["citation_free_hit_count"] == 2
        assert summary["citation_hit_rate_applicable"] == 0.0
        # 单条仍是空真 1.0（逐条语义未变）
        assert summary["citation_hit_rate"] == pytest.approx(1.0)

    def test_rows_carry_citation_expected_flag(self):
        result = run_eval.evaluate(self._mixed_golden(), [])
        flags = {row["id"]: row["citation_expected"] for row in result["rows"]}
        assert flags == {"C1": True, "C2": True, "C3": False, "C4": False}

    def test_real_golden_floor_is_quantified(self):
        """对真实 golden：空真地板必须在 summary 可见（R2-3 扩充后改为结构化断言）。"""
        golden = run_eval.load_jsonl(run_eval.DEFAULT_GOLDEN)
        summary = run_eval.evaluate(golden, [])["summary"]

        assert summary["total"] >= 100  # 指南 §2.3.7①：黄金集 100+
        assert summary["citation_expected_count"] > 0
        # 分母结构自洽：期望引用条数 + 空真条数 = 总数
        assert (
            summary["citation_expected_count"] + summary["citation_free_hit_count"]
            == summary["total"]
        )
        # 空预测下全量均值恰为空真地板
        assert summary["citation_hit_rate"] == pytest.approx(
            summary["citation_free_hit_count"] / summary["total"], abs=1e-4
        )
        assert summary["citation_hit_rate_applicable"] == pytest.approx(0.0)
        # R2-3：引用正确性聚合同样自洽（expected_citations 为真实分母）
        assert summary["citation_correctness_expected_count"] > 0
        assert summary["citation_correctness_applicable"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# score_must_contain：golden 的 must_contain 字段此前无任何消费方
#
# golden 里 61/102 条声明了 must_contain（如 L009 的 ["撤销"]），但在此修复之前
# 没有任何代码读取它——断言写在数据里没人看，等价于没写。以下锁定其评分口径
# 与「真的参与了 pass^k 判定」这两件事。
# ---------------------------------------------------------------------------


class TestMustContainScore:
    def test_no_requirement_is_vacuous_one(self):
        assert run_eval.score_must_contain([], "任意答案") == pytest.approx(1.0)
        assert run_eval.score_must_contain(None, "") == pytest.approx(1.0)

    def test_hit_and_miss(self):
        assert run_eval.score_must_contain(
            ["撤销"], "可以请求撤销该合同。"
        ) == pytest.approx(1.0)
        assert run_eval.score_must_contain(
            ["撤销"], "可以请求确认无效。"
        ) == pytest.approx(0.0)

    def test_partial_hit_is_fraction(self):
        # 答案含「撤销」但不含「胁迫」⇒ 1/2
        assert run_eval.score_must_contain(
            ["撤销", "胁迫"], "因欺诈订立的合同可以撤销。"
        ) == pytest.approx(0.5)

    def test_case_insensitive_for_ascii_keywords(self):
        assert run_eval.score_must_contain(["PDF"], "请上传 pdf 文件") == pytest.approx(1.0)

    def test_blank_keywords_are_ignored(self):
        """空白关键词不算要求，否则会变成一条永远拿不到的 0 分。"""
        assert run_eval.score_must_contain(
            ["", "   ", "撤销"], "可以撤销"
        ) == pytest.approx(1.0)

    def test_none_answer_scores_zero_when_required(self):
        assert run_eval.score_must_contain(["撤销"], None) == pytest.approx(0.0)

    def test_matches_answer_text_only_not_sources(self):
        """关键词只约束答案文本；sources 命中由 citation 系列负责，不在此处顶替。"""
        assert run_eval.score_must_contain(["撤销"], "") == pytest.approx(0.0)


class TestMustContainWiring:
    """must_contain 必须真正参与 pass^k 判定，而不只是被记录下来。"""

    def _golden(self, keywords):
        return [
            {
                "id": "M1",
                "question": "问题",
                "route_type": "kb-query",
                "expected_sources": [],
                "must_contain": keywords,
                "expect_reject": False,
            }
        ]

    def _pred(self, message):
        return [
            {
                "id": "M1",
                "route_type": "kb-query",
                "sources": [],
                "message": message,
                "status": "ok",
            }
        ]

    def test_missing_keyword_fails_task(self):
        assert (
            run_eval.compute_pass_k(self._golden(["撤销"]), self._pred("可以请求无效。"))[
                "pass_k"
            ]
            == 0.0
        )

    def test_present_keyword_passes_task(self):
        assert (
            run_eval.compute_pass_k(self._golden(["撤销"]), self._pred("可以请求撤销。"))[
                "pass_k"
            ]
            == 1.0
        )

    def test_no_keyword_requirement_unchanged(self):
        """无关键词要求时不得影响原判定（旧 golden 行为逐位一致）。"""
        assert (
            run_eval.compute_pass_k(self._golden([]), self._pred("随便答"))["pass_k"] == 1.0
        )

    def test_summary_exposes_must_contain_denominator(self):
        golden = self._golden(["撤销"]) + [
            {
                "id": "M2",
                "question": "闲聊",
                "route_type": "general-query",
                "expected_sources": [],
                "must_contain": [],
                "expect_reject": False,
            }
        ]
        preds = self._pred("可以请求撤销。") + [
            {
                "id": "M2",
                "route_type": "general-query",
                "sources": [],
                "message": "你好",
                "status": "ok",
            }
        ]
        s = run_eval.evaluate(golden, preds)["summary"]
        assert s["must_contain_expected_count"] == 1
        assert s["must_contain_free_hit_count"] == 1
        assert s["must_contain_applicable"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# pass_k 的「无数据 ≠ 全失败」语义
# ---------------------------------------------------------------------------


class TestPassKNoDataSemantics:
    def _golden(self):
        return [
            {
                "id": "N1",
                "question": "问题",
                "route_type": "kb-query",
                "expected_sources": [],
                "must_contain": [],
                "expect_reject": False,
            }
        ]

    def test_no_predictions_returns_none_not_zero(self):
        """预测为空 ⇒ 指标为 None（无数据），绝不能是 0.0（全部失败）。"""
        m = run_eval.compute_pass_k(self._golden(), [])
        assert m["pass_k"] is None
        assert m["pass_at_k"] is None
        assert m["mean_task_success"] is None
        assert m["pass_k_applicable"] is False
        assert m["pass_k_tasks"] == 0
        assert m["pass_k_tasks_without_predictions"] == 1
        assert "no_predictions" in m["pass_k_unavailable_reason"]

    def test_all_runs_failed_is_still_zero_not_none(self):
        """真正的「全部失败」仍必须是 0.0——不能与「没跑」混为一谈。"""
        preds = [
            {
                "id": "N1",
                "route_type": "graphrag-query",
                "sources": [],
                "message": "错",
                "status": "ok",
            }
        ]
        m = run_eval.compute_pass_k(self._golden(), preds)
        assert m["pass_k"] == 0.0
        assert m["pass_k_applicable"] is True
        assert m["pass_k_unavailable_reason"] is None

    def test_summary_carries_applicability_flag(self):
        s = run_eval.evaluate(self._golden(), [])["summary"]
        assert s["pass_k_applicable"] is False
        assert s["pass_k"] is None

    def test_format_line_distinguishes_no_data_from_zero(self):
        s = run_eval.evaluate(self._golden(), [])["summary"]
        line = run_eval.format_summary_line(s)
        assert "无数据" in line or "no_predictions" in line


# ---------------------------------------------------------------------------
# 拒答指标的适用口径
#
# golden 里 expect_reject=True 的只有 13/102。旧写法用 total 作分母，
# 其余条目「不误拒即算对」，把拒答指标抬到 ≈1-13/102 ≈ 0.8725 的地板，
# 对「越界问题有没有被正确拒掉」几乎没有区分度。
# ---------------------------------------------------------------------------


class TestRejectApplicableDenominator:
    def _golden(self):
        rows = []
        for i in range(4):  # 1 条越界 + 3 条非越界
            rows.append(
                {
                    "id": f"R{i}",
                    "question": f"q{i}",
                    "route_type": "general-query",
                    "expected_sources": [],
                    "must_contain": [],
                    "expect_reject": i == 0,
                }
            )
        return rows

    def test_applicable_denominator_only_counts_reject_rows(self):
        # 越界那条答了（错）；非越界三条正常答（对）
        preds = [
            {"id": "R0", "route_type": "general-query", "sources": [], "message": "这是实质回答", "status": "ok"},
            {"id": "R1", "route_type": "general-query", "sources": [], "message": "你好", "status": "ok"},
            {"id": "R2", "route_type": "general-query", "sources": [], "message": "你好", "status": "ok"},
            {"id": "R3", "route_type": "general-query", "sources": [], "message": "你好", "status": "ok"},
        ]
        s = run_eval.evaluate(self._golden(), preds)["summary"]

        # 全量口径：3/4 正确 —— 被非越界样本抬高
        assert s["reject_accuracy"] == pytest.approx(0.75)
        # 适用口径：0/1 —— 真实反映「该拒的没拒」
        assert s["reject_accuracy_applicable"] == pytest.approx(0.0)
        assert s["reject_expected_count"] == 1
        assert s["reject_false_refusal_rate"] == pytest.approx(0.0)

    def test_false_refusal_rate_surfaces_over_refusal(self):
        preds = [
            {"id": "R0", "route_type": "general-query", "sources": [], "message": "抱歉，无法回答。", "status": "rejected"},
            {"id": "R1", "route_type": "general-query", "sources": [], "message": "抱歉，无法回答。", "status": "rejected"},
            {"id": "R2", "route_type": "general-query", "sources": [], "message": "你好", "status": "ok"},
            {"id": "R3", "route_type": "general-query", "sources": [], "message": "你好", "status": "ok"},
        ]
        s = run_eval.evaluate(self._golden(), preds)["summary"]
        assert s["reject_accuracy_applicable"] == pytest.approx(1.0)
        # 3 条非越界里有 1 条被误拒
        assert s["reject_false_refusal_rate"] == pytest.approx(1 / 3, abs=1e-4)

    def test_zero_denominator_yields_zero_not_one(self):
        golden = [
            {
                "id": "Z1",
                "question": "闲聊",
                "route_type": "general-query",
                "expected_sources": [],
                "must_contain": [],
                "expect_reject": False,
            }
        ]
        preds = [
            {
                "id": "Z1",
                "route_type": "general-query",
                "sources": [],
                "message": "你好",
                "status": "ok",
            }
        ]
        s = run_eval.evaluate(golden, preds)["summary"]
        assert s["reject_expected_count"] == 0
        assert s["reject_accuracy_applicable"] == 0.0


def test_csv_fields_cover_new_per_sample_columns():
    """CSV 必须承载新增的逐条明细，否则复核时会静默丢列。"""
    for field in run_eval.CSV_FIELDS:
        assert isinstance(field, str) and field
    assert "must_contain_score" in run_eval.CSV_FIELDS
    assert "citation_correctness" in run_eval.CSV_FIELDS
    assert "expect_reject" in run_eval.CSV_FIELDS
