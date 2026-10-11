"""Phase 26：评估三期——judge 去 stub + `--compare` 配对显著性。

覆盖 DoD：
* judge：有端点出分且带 model 标注；无端点诚实失败（NotImplementedError）；
  默认路径（--no-judge）不触发；输出不可解析 → JudgeError（不给假分）；
  summary 仅新增 ``judge_*`` 前缀键（五 metric 门结构零变化）。
* ``--compare``：同分布不显著（噪声区间）、构造差异分布显著——
  离线构造数据，非伪造 baseline。

⚠️ 本地 ``.env`` 可能带真实端点凭据——所有用例显式 monkeypatch
端点设置 + 伪造 LLMClient，**禁止触网**。
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

FIVE_GATE_METRICS = (
    "route_accuracy",
    "citation_hit_rate",
    "reject_accuracy",
    "must_contain",
    "citation_correctness",
)


def _load_run_eval():
    spec = importlib.util.spec_from_file_location(
        "p26_run_eval", EVALS_DIR / "run_eval.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["p26_run_eval"] = module
    spec.loader.exec_module(module)
    return module


run_eval = _load_run_eval()


# ---------------------------------------------------------------------------
# 构造离线数据（真实 baseline 的替身，仅测试内构造——非伪造仓库基线）
# ---------------------------------------------------------------------------


def _golden_row(gid: str, question: str, route: str = "kb-query") -> Dict[str, Any]:
    return {
        "id": gid,
        "question": question,
        "route_type": route,
        "expect_reject": False,
        "expected_sources": [],
        "must_contain": [],
    }


def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def _pred(gid: str, *, ok: bool = True, route: str = "kb-query") -> Dict[str, Any]:
    return {
        "id": gid,
        "route_type": route if ok else "chat",
        "sources": [],
        "message": "这是一段针对问题的实质回答。" if ok else "嗯嗯。",
        "status": "ok",
    }


def _fake_llm_module(monkeypatch, reply: str) -> List[Dict[str, Any]]:
    """伪造 LLMClient：记录构造参数，chat 返回指定文本。"""
    import agentix.application.services.llm_client as llm_mod

    calls: List[Dict[str, Any]] = []

    class _FakeLLMClient:
        def __init__(self, *, model=None, temperature=0.7, **kwargs):
            calls.append({"model": model, "temperature": temperature})

        async def chat(self, *, system_prompt, user_message, context=None,
                       temperature=None):
            calls.append({"system": system_prompt, "user": user_message})
            return reply

    monkeypatch.setattr(llm_mod, "LLMClient", _FakeLLMClient)
    return calls


def _set_endpoint(monkeypatch, *, key: str = "test-key",
                  base: str = "http://127.0.0.1:9/v1") -> None:
    from agentix.config import settings

    monkeypatch.setattr(settings, "LLM_API_KEY", key)
    monkeypatch.setattr(settings, "LLM_BASE_URL", base)


def _clear_endpoint(monkeypatch) -> None:
    from agentix.config import settings

    monkeypatch.setattr(settings, "LLM_API_KEY", "")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "")


# ---------------------------------------------------------------------------
# judge：出分 / 诚实失败 / 解析
# ---------------------------------------------------------------------------


def test_judge_scores_with_fake_client_and_model_annotation(monkeypatch):
    """有端点 + 伪 client → 出分；构造参数带解析后的模型名与温度 0。"""
    _set_endpoint(monkeypatch)
    calls = _fake_llm_module(monkeypatch, "0.87")

    score = run_eval.judge_similarity("什么是违约金？", "违约金是……")

    assert score == pytest.approx(0.87)
    from agentix.infrastructure.llm.model_gateway import get_model_gateway

    expected_model = get_model_gateway().resolve_model("standard")
    ctor = calls[0]
    assert ctor["model"] == expected_model
    assert ctor["temperature"] == 0.0


def test_judge_empty_answer_returns_zero_without_any_client(monkeypatch):
    """空回答 → 确定性 0.0（不预检、不构造 client、不触网）。"""
    _clear_endpoint(monkeypatch)
    calls = _fake_llm_module(monkeypatch, "0.9")
    assert run_eval.judge_similarity("问题", "") == 0.0
    assert calls == []


def test_judge_without_endpoint_raises_not_implemented(monkeypatch):
    """无端点 → NotImplementedError 诚实失败（绝不给假分）。"""
    _clear_endpoint(monkeypatch)
    with pytest.raises(NotImplementedError, match="校准"):
        run_eval.judge_similarity("问题", "回答")


def test_judge_unparseable_output_raises_judge_error(monkeypatch):
    """评委输出不可解析 → JudgeError（拒绝猜分/假分）。"""
    _set_endpoint(monkeypatch)
    _fake_llm_module(monkeypatch, "这个回答还挺好的，很难量化")
    with pytest.raises(run_eval.JudgeError, match="拒绝给假分"):
        run_eval.judge_similarity("问题", "回答")


def test_parse_judge_score_bounds():
    assert run_eval._parse_judge_score("0.87") == pytest.approx(0.87)
    assert run_eval._parse_judge_score("得分：0.42") == pytest.approx(0.42)
    assert run_eval._parse_judge_score("1.0") == pytest.approx(1.0)
    assert run_eval._parse_judge_score("1") == pytest.approx(1.0)
    with pytest.raises(run_eval.JudgeError):
        run_eval._parse_judge_score("95 分（满分 100）")
    with pytest.raises(run_eval.JudgeError):
        run_eval._parse_judge_score("")


# ---------------------------------------------------------------------------
# evaluate / summary / format：judge_* 只增不改
# ---------------------------------------------------------------------------


def test_evaluate_judge_summary_annotation(monkeypatch):
    """use_judge=True → summary 附 judge_model/count/mean；五 metric 键不变。"""
    _set_endpoint(monkeypatch)
    _fake_llm_module(monkeypatch, "0.75")

    golden = [_golden_row("T1", "问题一"), _golden_row("T2", "问题二")]
    preds = [_pred("T1"), _pred("T2")]
    result = run_eval.evaluate(golden, preds, use_judge=True)
    summary = result["summary"]

    for key in FIVE_GATE_METRICS:
        assert key in summary, f"门禁 metric 键缺失：{key}"
    assert summary["judge_enabled"] is True
    assert summary["judge_count"] == 2
    assert summary["judge_mean"] == pytest.approx(0.75)
    assert isinstance(summary["judge_model"], str) and summary["judge_model"]
    # judge 相关键全部 judge_* 前缀，不污染既有键名空间
    judge_keys = [k for k in summary if k.startswith("judge")]
    assert set(judge_keys) <= {
        "judge_enabled", "judge_model", "judge_count", "judge_mean"
    }
    # 逐条也有 judge_similarity
    assert all(row.get("judge_similarity") == pytest.approx(0.75)
               for row in result["rows"])


def test_evaluate_without_judge_keeps_zero_judge_side_effects(monkeypatch):
    """默认 use_judge=False → 零 judge 键行为（除 enabled=False）不构造 client。"""
    calls = _fake_llm_module(monkeypatch, "0.5")
    _clear_endpoint(monkeypatch)  # 若真去构造 client，预检也会拦下

    golden = [_golden_row("T1", "问题一")]
    summary = run_eval.evaluate(golden, [_pred("T1")])["summary"]
    assert summary["judge_enabled"] is False
    assert summary["judge_model"] is None
    assert summary["judge_count"] == 0
    assert summary["judge_mean"] is None
    assert calls == []


def test_format_summary_line_judge_annotation():
    summary = {
        "total": 2,
        "route_accuracy": 1.0,
        "citation_hit_rate": 1.0,
        "reject_accuracy": 1.0,
        "must_contain": 1.0,
        "citation_correctness": 1.0,
        "judge_enabled": True,
        "judge_model": "deepseek-chat",
        "judge_mean": 0.8123,
        "judge_count": 2,
    }
    line = run_eval.format_summary_line(summary)
    assert "辅助指标" in line
    assert "0.8123" in line
    assert "deepseek-chat" in line
    assert "不进门禁" in line


def test_default_offline_path_never_builds_judge_client(monkeypatch):
    """默认离线跑分全程不触碰 judge（伪 client 零构造、零端点依赖）。"""
    calls = _fake_llm_module(monkeypatch, "0.99")
    _clear_endpoint(monkeypatch)

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _write_jsonl(tmp_path / "golden.jsonl", [_golden_row("T1", "问题一")])
        _write_jsonl(tmp_path / "preds.jsonl", [_pred("T1")])
        code = run_eval.run_offline(
            golden_path=tmp_path / "golden.jsonl",
            predictions_path=tmp_path / "preds.jsonl",
            results_dir=tmp_path / "results",
        )
        assert code == 0
    assert calls == []


def test_cli_judge_unparseable_exits_3(tmp_path, capsys, monkeypatch):
    """judge 输出不可解析 → main 捕获 JudgeError → 退出码 3 + 中文错误。"""
    _set_endpoint(monkeypatch)
    _fake_llm_module(monkeypatch, "很难评")
    _write_jsonl(tmp_path / "golden.jsonl", [_golden_row("T1", "问题一")])
    _write_jsonl(tmp_path / "preds.jsonl", [_pred("T1")])

    code = run_eval.main(
        [
            "--offline",
            "--judge",
            "--golden", str(tmp_path / "golden.jsonl"),
            "--predictions", str(tmp_path / "preds.jsonl"),
            "--results-dir", str(tmp_path / "results"),
        ]
    )
    assert code == 3
    assert "拒绝给假分" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# --compare：percentile bootstrap 配对显著性
# ---------------------------------------------------------------------------


def test_bootstrap_ci_deterministic_and_bounds():
    diffs = [0.0] * 10
    low, high = run_eval._bootstrap_percentile_ci(diffs)
    assert (low, high) == (0.0, 0.0)

    diffs2 = [1.0, 1.0, 1.0, 1.0]
    low2, high2 = run_eval._bootstrap_percentile_ci(diffs2)
    assert low2 == pytest.approx(1.0)
    assert high2 == pytest.approx(1.0)

    # 固定 seed 可复现
    mixed = [0.2, -0.1, 0.05, 0.0, 0.3, -0.2, 0.1, 0.0]
    a = run_eval._bootstrap_percentile_ci(mixed)
    b = run_eval._bootstrap_percentile_ci(mixed)
    assert a == b

    # 单样本不可判定
    assert run_eval._bootstrap_percentile_ci([0.5]) == (None, None)


def test_compare_identical_distribution_not_significant():
    """同分布（逐条相同）→ Δ=0、CI=[0,0] → 不显著（噪声区间）。"""
    golden = [_golden_row(f"T{i}", f"问题{i}") for i in range(12)]
    preds = [_pred(f"T{i}") for i in range(12)]
    rows = run_eval.evaluate(golden, preds)["rows"]

    report = run_eval.compare_rows(rows, rows)
    assert report["paired_count"] == 12
    for key, _label in run_eval.COMPARE_METRICS:
        m = report["metrics"][key]
        assert m["delta"] == pytest.approx(0.0)
        assert m["significant"] is False, f"{key} 同分布不应显著"
    assert "不显著（噪声区间）" in run_eval.format_compare_report(report)


def test_compare_different_distribution_significant():
    """构造差异分布（A 全对 vs B 大量错）→ 关键指标显著。"""
    golden = [_golden_row(f"T{i}", f"问题{i}") for i in range(12)]
    preds_a = [_pred(f"T{i}", ok=True) for i in range(12)]
    preds_b = [
        _pred(f"T{i}", ok=(i % 4 == 0)) for i in range(12)
    ]
    rows_a = run_eval.evaluate(golden, preds_a)["rows"]
    rows_b = run_eval.evaluate(golden, preds_b)["rows"]

    report = run_eval.compare_rows(rows_a, rows_b)
    assert report["paired_count"] == 12
    route = report["metrics"]["route_ok"]
    assert route["delta"] > 0
    assert route["significant"] is True
    report_text = run_eval.format_compare_report(report)
    assert "显著" in report_text
    assert "噪声区间" not in report_text.split("\n")[1]


def test_run_compare_end_to_end_and_cli(tmp_path, capsys):
    """run_compare 离线端到端：退出 0 + 报告含配对数与显著性结论。"""
    golden_path = _write_jsonl(
        tmp_path / "golden.jsonl",
        [_golden_row(f"T{i}", f"问题{i}") for i in range(10)],
    )
    path_a = _write_jsonl(
        tmp_path / "a.jsonl", [_pred(f"T{i}", ok=True) for i in range(10)]
    )
    path_b = _write_jsonl(
        tmp_path / "b.jsonl",
        [_pred(f"T{i}", ok=(i % 2 == 0)) for i in range(10)],
    )

    code = run_eval.run_compare(path_a, path_b, golden_path=golden_path)
    out = capsys.readouterr().out
    assert code == 0
    assert "配对样本 10 条" in out
    assert "95% CI" in out

    # CLI 分发
    code2 = run_eval.main(
        ["--compare", str(path_a), str(path_b), "--golden", str(golden_path)]
    )
    assert code2 == 0


def test_run_compare_missing_file_returns_2(tmp_path, capsys):
    golden_path = _write_jsonl(
        tmp_path / "golden.jsonl", [_golden_row("T1", "问题")]
    )
    code = run_eval.run_compare(
        tmp_path / "a.jsonl", tmp_path / "nope.jsonl", golden_path=golden_path
    )
    assert code == 2
    assert "未找到预测文件" in capsys.readouterr().err


def test_run_compare_empty_golden_returns_2(tmp_path, capsys):
    """golden 为空 → 无任何配对 id → 退出 2，不给不可判定的结论。"""
    golden_path = _write_jsonl(tmp_path / "golden.jsonl", [])
    path_a = _write_jsonl(tmp_path / "a.jsonl", [_pred("T1")])
    path_b = _write_jsonl(tmp_path / "b.jsonl", [_pred("T1")])

    code = run_eval.run_compare(path_a, path_b, golden_path=golden_path)
    assert code == 2
    assert "无法配对" in capsys.readouterr().err


def test_compare_rows_disjoint_ids_reported():
    """id 交集为空 → 配对 0 + 各边独有数如实报告（不静默丢弃）。"""
    report = run_eval.compare_rows(
        [{"id": "X1", "route_ok": 1.0}],
        [{"id": "Y1", "route_ok": 1.0}],
    )
    assert report["paired_count"] == 0
    assert report["a_only_count"] == 1
    assert report["b_only_count"] == 1
    assert "样本不足" in run_eval.format_compare_report(report)


def test_compare_with_live_flags_rejected(tmp_path, capsys):
    """--compare 不能与 --live/--record 同用（只支持离线打分）。"""
    code = run_eval.main(
        ["--compare", "a.jsonl", "b.jsonl", "--live"]
    )
    assert code == 2
    assert "离线打分" in capsys.readouterr().err


def test_compare_parser_defaults():
    parser = run_eval.build_parser()
    assert parser.parse_args([]).compare is None
    args = parser.parse_args(["--compare", "a.jsonl", "b.jsonl"])
    assert args.compare == ["a.jsonl", "b.jsonl"]
