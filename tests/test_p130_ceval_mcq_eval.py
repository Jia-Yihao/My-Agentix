"""公开 MCQ 评测集（C-Eval → golden）的转换与判分口径。

1. **转换器的回源校验是硬门**：写完重开文件、重新加载源数据逐行比对，
   不一致退出码 2——「校验失败也照样出文件」等于没校验。
2. **抽样可复现**：同 seed 两次抽样结果逐行相同；不同学科条目互不串味。
3. **判分用选项字母抽取**，不用子串命中：显式标注 → 行首字母 → 选项原文反推
   → 抽不出记 ``none``；四种方式各自计数，宽松口径占比看得见。
4. **诚实缺席**：扁平拓扑没有多 agent 元数据 ⇒ 报告里该组是「不适用」，
   不是 0（把「没有」读成 0 是这类报告最常见的撒谎方式）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.eval import ceval_to_golden  # noqa: E402
from scripts.eval.mcq_report import build_report, extract_letter  # noqa: E402


# ---------------------------------------------------------------------------
# 1. 回源校验：必须真的逐行比对
# ---------------------------------------------------------------------------


class TestBackVerification:
    @pytest.fixture()
    def source_row(self) -> Dict[str, Any]:
        return {
            "id": 1,
            "question": "题干",
            "A": "正确项",
            "B": "错误项",
            "C": "第三个",
            "D": "第四个",
            "answer": "A",
        }

    @pytest.fixture()
    def golden_file(self, tmp_path: Path) -> Path:
        golden = tmp_path / "g.jsonl"
        row = {
            "id": "ceval-x-1",
            "question": "题干\nA. 正确项\nB. 错误项\nC. 第三个\nD. 第四个\n"
            "请只回答正确选项的字母和对应内容，例如：答案 D. <选项内容>",
            "route_type": "general-query",
            "expected_sources": [],
            "must_contain": ["正确项"],
            "expect_reject": False,
            "source": "ceval/ceval-exam:x",
            "notes": json.dumps(
                {
                    "src_id": 1,
                    "src_split": "test",
                    "gold_letter": "A",
                    "options": {
                        "A": "正确项",
                        "B": "错误项",
                        "C": "第三个",
                        "D": "第四个",
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        }
        golden.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        return golden

    def test_consistent_file_passes(self, golden_file: Path, source_row, monkeypatch):
        monkeypatch.setattr(ceval_to_golden, "_load_subject_rows", lambda *a, **k: [source_row])
        result = ceval_to_golden.verify_against_source(golden_file, "ds", "test")
        assert result["rows_checked"] == 1

    def test_tampered_option_detected(self, golden_file: Path, source_row, monkeypatch):
        tampered = dict(source_row, A="被改过的正确项")
        monkeypatch.setattr(ceval_to_golden, "_load_subject_rows", lambda *a, **k: [tampered])
        with pytest.raises(ValueError):
            ceval_to_golden.verify_against_source(golden_file, "ds", "test")

    def test_tampered_answer_detected(self, golden_file: Path, source_row, monkeypatch):
        # 源数据集答案变了（answer B），文件里仍写 A ⇒ 答案不一致
        monkeypatch.setattr(
            ceval_to_golden, "_load_subject_rows", lambda *a, **k: [dict(source_row, answer="B")]
        )
        with pytest.raises(ValueError):
            ceval_to_golden.verify_against_source(golden_file, "ds", "test")

    def test_missing_source_row_detected(self, golden_file: Path, monkeypatch):
        monkeypatch.setattr(ceval_to_golden, "_load_subject_rows", lambda *a, **k: [])
        with pytest.raises(ValueError):
            ceval_to_golden.verify_against_source(golden_file, "ds", "test")


# ---------------------------------------------------------------------------
# 2. 抽样可复现
# ---------------------------------------------------------------------------


class TestSampling:
    def test_same_seed_same_rows(self):
        rows = [{"id": i, "question": f"q{i}", "answer": "A"} for i in range(20)]
        first = ceval_to_golden._sample_rows(rows, "law", 3, 7)
        second = ceval_to_golden._sample_rows(rows, "law", 3, 7)
        assert [r["id"] for r in first] == [r["id"] for r in second]
        assert len(first) == 3

    def test_different_subject_different_sample(self):
        rows = [{"id": i, "question": f"q{i}", "answer": "A"} for i in range(50)]
        law = ceval_to_golden._sample_rows(rows, "law", 3, 7)
        math = ceval_to_golden._sample_rows(rows, "math", 3, 7)
        assert [r["id"] for r in law] != [r["id"] for r in math]

    def test_per_subject_capped_by_pool_size(self):
        rows = [{"id": i, "question": f"q{i}", "answer": "A"} for i in range(2)]
        assert len(ceval_to_golden._sample_rows(rows, "law", 3, 1)) == 2


# ---------------------------------------------------------------------------
# 3. 选项字母抽取
# ---------------------------------------------------------------------------


_OPTIONS = {"A": "苹果", "B": "香蕉", "C": "橘子", "D": "葡萄"}


class TestExtractLetter:
    @pytest.mark.parametrize(
        "message",
        [
            "答案是 B",
            "答案：B",
            "答案为B。",
            "选 B",
            "正确选项是 B",
            "应选B",
        ],
    )
    def test_explicit_marker(self, message: str):
        assert extract_letter(message, _OPTIONS) == ("B", "letter")

    def test_line_start_letter(self):
        assert extract_letter("B. 香蕉是黄色的", _OPTIONS) == ("B", "letter")

    def test_text_fallback_only_when_no_letter(self):
        # 答案里出现了选项原文但没给字母 ⇒ 走 text 兜底
        assert extract_letter("我觉得应该是香蕉", _OPTIONS) == ("B", "text")

    def test_text_fallback_ambiguous_is_none(self):
        # 两个选项原文都出现 ⇒ 无法判定，不硬猜
        assert extract_letter("香蕉和苹果", _OPTIONS) == (None, "none")

    def test_unparseable_is_none(self):
        assert extract_letter("这道题需要结合教材上下文。", _OPTIONS) == (None, "none")

    def test_empty_message_is_none(self):
        assert extract_letter("", _OPTIONS) == (None, "none")

    def test_letter_wins_over_text(self):
        # 同时给了字母和选项原文 ⇒ 字母优先
        assert extract_letter("答案 C。橘子", _OPTIONS) == ("C", "letter")


# ---------------------------------------------------------------------------
# 4. 报告：诚实缺席
# ---------------------------------------------------------------------------


def _golden(gid: str, subject: str, gold: str, options: Dict[str, str]) -> Dict[str, Any]:
    return {
        "id": gid,
        "question": "题干",
        "route_type": "general-query",
        "expected_sources": [],
        "must_contain": [options[gold]],
        "expect_reject": False,
        "source": f"ceval/ceval-exam:{subject}",
        "notes": json.dumps(
            {"gold_letter": gold, "options": options, "src_id": 1, "src_split": "test"},
            ensure_ascii=False,
        ),
    }


def _pred(gid: str, message: str, **extra: Any) -> Dict[str, Any]:
    return {
        "id": gid,
        "attempt": 0,
        "route_type": "general-query",
        "sources": [],
        "message": message,
        "status": "ok",
        **extra,
    }


class TestReportHonesty:
    def test_flat_topology_multi_agent_not_applicable_not_zero(self):
        golden = [_golden("g1", "law", "A", _OPTIONS)]
        report = build_report(golden, [_pred("g1", "答案是 A")], "扁平")
        assert report["multi_agent"]["applicable"] is False
        assert "exit_reason_dist" not in report["multi_agent"]

    def test_supervisor_metadata_reported(self):
        golden = [_golden("g1", "law", "A", _OPTIONS)]
        pred = _pred(
            "g1",
            "答案是 A",
            multi_agent={
                "handler": "general_query",
                "ok": True,
                "rounds": 2,
                "failure_mode": None,
                "incremental_ratio": 0.5,
                "status": "completed",
                "exit_reason": "completed",
                "truncated": False,
            },
        )
        report = build_report(golden, [pred], "多 agent")
        assert report["multi_agent"]["applicable"] is True
        assert report["multi_agent"]["exit_reason_dist"] == {"completed": 1}
        assert report["multi_agent"]["rounds_mean"] == 2.0

    def test_unparsed_not_counted_as_correct(self):
        golden = [_golden("g1", "law", "A", _OPTIONS)]
        report = build_report(golden, [_pred("g1", "无从判断")], "x")
        assert report["mcq"]["parsed"] == 0
        assert report["mcq"]["correct"] == 0
        assert report["mcq"]["accuracy"] is None
        assert report["mcq"]["unparsed"] == 1

    def test_accuracy_denominator_is_parsed_only(self):
        golden = [
            _golden("g1", "law", "A", _OPTIONS),
            _golden("g2", "math", "B", _OPTIONS),
        ]
        preds = [_pred("g1", "答案是 A"), _pred("g2", "无从判断")]
        report = build_report(golden, preds, "x")
        assert report["mcq"]["parsed"] == 1
        assert report["mcq"]["correct"] == 1
        assert report["mcq"]["accuracy"] == 1.0

    def test_missing_perf_reported_as_not_collected(self):
        golden = [_golden("g1", "law", "A", _OPTIONS)]
        report = build_report(golden, [_pred("g1", "答案是 A")], "x")
        assert report["perf"]["latency_ms"]["n"] == 0
        assert report["perf"]["latency_ms"]["p50"] is None
        assert report["perf"]["llm_steps"]["mean"] is None

    def test_perf_collected_when_present(self):
        golden = [_golden("g1", "law", "A", _OPTIONS)]
        pred = _pred(
            "g1",
            "答案是 A",
            perf={"latency_ms": 1200.0, "llm_steps": 3, "total_tokens": 100},
        )
        report = build_report(golden, [pred], "x")
        assert report["perf"]["latency_ms"]["p50"] == 1200.0
        assert report["perf"]["llm_steps"]["mean"] == 3.0

    def test_prediction_without_golden_row_is_ignored(self):
        golden = [_golden("g1", "law", "A", _OPTIONS)]
        preds = [_pred("g1", "答案是 A"), _pred("unknown", "答案是 A")]
        report = build_report(golden, preds, "x")
        assert report["mcq"]["correct"] == 1
        assert report["rows"] == 2  # 行数照实，但只有一条可判分


# ---------------------------------------------------------------------------
# 5. 预测行形状：perf 段不破坏既有字段
# ---------------------------------------------------------------------------


class TestPredictionRowShape:
    def test_perf_pick_returns_latency_always(self):
        from evals import run_eval

        perf = run_eval._pick_perf({}, 12.34)
        assert perf["latency_ms"] == 12.34
        assert "llm_steps" not in perf

    def test_perf_pick_counts_llm_steps_and_tokens(self):
        from evals import run_eval

        response = {
            "metadata": {
                "runtime": {
                    "steps": [
                        {
                            "step_type": "llm",
                            "latency_ms": 100,
                            "output": {"prompt_tokens": 10, "completion_tokens": 5},
                        },
                        {"step_type": "tool", "latency_ms": 5, "output": {}},
                        {
                            "step_type": "llm_call",
                            "latency_ms": 50,
                            "output": {"total_tokens": 20},
                        },
                    ]
                }
            }
        }
        perf = run_eval._pick_perf(response, 500.0)
        assert perf["llm_steps"] == 2
        assert perf["llm_latency_ms"] == 150.0
        assert perf["prompt_tokens"] == 10
        assert perf["completion_tokens"] == 5
        assert perf["total_tokens"] == 20


# ---------------------------------------------------------------------------
# 6. 仓内那份金标集的形状（存在性 + 口径）
# ---------------------------------------------------------------------------


class TestShippedGolden:
    @pytest.fixture(scope="class")
    def shipped(self) -> List[Dict[str, Any]]:
        path = REPO_ROOT / "evals" / "ceval_golden.jsonl"
        if not path.exists():
            pytest.skip("仓内未落 C-Eval 金标（需先跑转换脚本）")
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_rows_non_empty(self, shipped: List[Dict[str, Any]]):
        assert shipped

    def test_every_row_has_required_fields(self, shipped: List[Dict[str, Any]]):
        for row in shipped:
            assert row["id"]
            assert row["question"]
            assert row["route_type"] == "general-query"
            assert isinstance(row["must_contain"], list)
            notes = json.loads(row["notes"])
            assert notes["gold_letter"] in ("A", "B", "C", "D")
            assert set(notes["options"]) == {"A", "B", "C", "D"}

    def test_must_contain_matches_gold_option(self, shipped: List[Dict[str, Any]]):
        for row in shipped:
            notes = json.loads(row["notes"])
            assert row["must_contain"] == [notes["options"][notes["gold_letter"]]]

    def test_manifest_records_license_and_provenance(self):
        path = REPO_ROOT / "evals" / "ceval_golden.manifest.json"
        if not path.exists():
            pytest.skip("仓内未落 manifest")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        assert manifest["dataset"] == "ceval/ceval-exam"
        assert manifest["license"] == "cc-by-nc-sa-4.0"  # 非商用
        assert manifest["rows"] > 0
        assert manifest["verification"]["rows_checked"] == manifest["rows"]
