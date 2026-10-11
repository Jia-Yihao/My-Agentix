"""Stage T · evals 退出契约报告键（总纲 §6 Multi-Agent 四键中的三键）。

1. ``_aggregate_multi_agent_exit`` 独立于 ``_aggregate_multi_agent``
   （p88「评测面零接触」断言只测后者，互不污染）：
   dist / budget_exhaust_rate / cancel_terminal_rate(+applicable) 精确口径。
2. meta 缺 ``exit_reason``（Phase 19 旧预测行 / 扁平拓扑）⇒ ``{}``，
   evaluate() summary 不加本组键——不把「没有」读成 0。
3. 只增报告键纪律：新键不进 CI ``METRICS`` 五键门（``ci.yml`` 交集为空），
   ``test_ci_gate_contract`` / phase31 空预测快照与前缀白名单零变化。
4. 取消口径：分母 = ``exit_reason == "interrupted"`` 的样本；分子 = 其中
   ``status`` 落在明确终态集合（completed / interrupted / failed）的占比；
   无取消样本 ⇒ rate=None + applicable=False（离线不造假）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CI_PATH = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"

from evals import run_eval  # noqa: E402  —— pythonpath=["."] 包路径加载


def _meta(exit_reason: str, status: str, **extra: Any) -> Dict[str, Any]:
    return {"exit_reason": exit_reason, "status": status, **extra}


def _rows(*metas: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"multi_agent": dict(m)} for m in metas]


# ---------------------------------------------------------------------------
# 1. 三键精确口径
# ---------------------------------------------------------------------------


class TestExitReportKeys:
    def test_dist_counts_all_four_reasons(self):
        agg = run_eval._aggregate_multi_agent_exit(
            _rows(
                _meta("completed", "completed"),
                _meta("completed", "completed"),
                _meta("max_iterations", "failed"),
                _meta("interrupted", "interrupted"),
                _meta("error", "failed"),
            )
        )
        assert agg["multi_agent_exit_reason_dist"] == {
            "completed": 2,
            "error": 1,
            "interrupted": 1,
            "max_iterations": 1,
        }

    def test_budget_exhaust_rate_is_max_iterations_share(self):
        agg = run_eval._aggregate_multi_agent_exit(
            _rows(
                _meta("completed", "completed"),
                _meta("completed", "completed"),
                _meta("max_iterations", "failed"),
                _meta("error", "failed"),
            )
        )
        assert agg["multi_agent_budget_exhaust_rate"] == 0.25

    def test_budget_exhaust_rate_zero_when_none_exhausted(self):
        agg = run_eval._aggregate_multi_agent_exit(
            _rows(_meta("completed", "completed"), _meta("error", "failed"))
        )
        assert agg["multi_agent_budget_exhaust_rate"] == 0.0

    def test_cancel_rate_full_when_all_cancelled_reached_terminal(self):
        agg = run_eval._aggregate_multi_agent_exit(
            _rows(
                _meta("interrupted", "interrupted"),
                _meta("interrupted", "interrupted"),
            )
        )
        assert agg["multi_agent_cancel_terminal_applicable"] is True
        assert agg["multi_agent_cancel_terminal_rate"] == 1.0

    def test_cancel_rate_counts_only_known_terminal_status(self):
        agg = run_eval._aggregate_multi_agent_exit(
            _rows(
                _meta("interrupted", "interrupted"),
                _meta("interrupted", "exploring"),  # 未知 status ≠ 终态
            )
        )
        assert agg["multi_agent_cancel_terminal_rate"] == 0.5

    def test_cancel_rate_none_without_cancel_samples(self):
        agg = run_eval._aggregate_multi_agent_exit(
            _rows(
                _meta("completed", "completed"),
                _meta("error", "failed"),
            )
        )
        assert agg["multi_agent_cancel_terminal_applicable"] is False
        assert agg["multi_agent_cancel_terminal_rate"] is None

    def test_keys_sorted_and_float_rate(self):
        agg = run_eval._aggregate_multi_agent_exit(
            _rows(_meta("max_iterations", "failed"), _meta("error", "failed"))
        )
        assert agg["multi_agent_exit_reason_dist"] == {
            "error": 1,
            "max_iterations": 1,
        }
        assert isinstance(agg["multi_agent_budget_exhaust_rate"], float)


# ---------------------------------------------------------------------------
# 2. 缺键 / 空输入诚实缺席
# ---------------------------------------------------------------------------


class TestAbsentInputs:
    def test_meta_without_exit_reason_returns_empty(self):
        # Phase 19 形状：既有键齐、无退出契约键 ⇒ 本组键整体缺席
        legacy = {"ok": True, "rounds": 1, "failure_mode": "none"}
        assert run_eval._aggregate_multi_agent_exit(
            [{"multi_agent": legacy}]
        ) == {}

    def test_empty_rows_returns_empty(self):
        assert run_eval._aggregate_multi_agent_exit([]) == {}

    def test_non_dict_multi_agent_skipped(self):
        assert run_eval._aggregate_multi_agent_exit(
            [{"multi_agent": None}, {"id": "x"}]
        ) == {}

    def test_fully_qualified_reason_but_missing_status_is_not_terminal(self):
        # interrupted 取消样本但 status 缺失 ⇒ 分母在、分子不计
        agg = run_eval._aggregate_multi_agent_exit(
            [{"multi_agent": {"exit_reason": "interrupted"}}]
        )
        assert agg["multi_agent_cancel_terminal_applicable"] is True
        assert agg["multi_agent_cancel_terminal_rate"] == 0.0


# ---------------------------------------------------------------------------
# 3. evaluate() 接线：有 meta 出键、无 meta 正交缺席
# ---------------------------------------------------------------------------


def _golden(gid: str) -> Dict[str, Any]:
    return {
        "id": gid,
        "question": "问题",
        "route_type": "kb-query",
        "expected_sources": [],
        "must_contain": [],
        "expect_reject": False,
    }


def _pred(gid: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": gid,
        "route_type": "kb-query",
        "sources": [],
        "message": "ok",
        "status": "ok",
        "multi_agent": dict(meta),
    }


class TestEvaluateWiring:
    def test_evaluate_exposes_exit_keys_when_meta_present(self):
        summary = run_eval.evaluate(
            [_golden("E1")],
            [
                _pred(
                    "E1",
                    {
                        "ok": True,
                        "rounds": 1,
                        "failure_mode": "none",
                        "incremental_ratio": 1.0,
                        "status": "completed",
                        "exit_reason": "completed",
                        "truncated": False,
                    },
                )
            ],
        )["summary"]
        assert summary["multi_agent_exit_reason_dist"] == {"completed": 1}
        assert summary["multi_agent_budget_exhaust_rate"] == 0.0
        assert summary["multi_agent_cancel_terminal_applicable"] is False
        assert summary["multi_agent_cancel_terminal_rate"] is None
        # 既有 Phase 19 键不受影响
        assert summary["multi_agent_ok_rate"] == 1.0

    def test_evaluate_without_meta_keeps_group_absent(self):
        summary = run_eval.evaluate(
            [_golden("E1")],
            [
                {
                    "id": "E1",
                    "route_type": "kb-query",
                    "sources": [],
                    "message": "ok",
                    "status": "ok",
                }
            ],
        )["summary"]
        assert "multi_agent_exit_reason_dist" not in summary
        assert summary["multi_agent_meta_count"] == 0

    def test_real_golden_empty_predictions_keeps_group_absent(self):
        summary = run_eval.evaluate(
            run_eval.load_jsonl(run_eval.DEFAULT_GOLDEN), []
        )["summary"]
        for key in (
            "multi_agent_exit_reason_dist",
            "multi_agent_budget_exhaust_rate",
            "multi_agent_cancel_terminal_applicable",
            "multi_agent_cancel_terminal_rate",
        ):
            assert key not in summary


# ---------------------------------------------------------------------------
# 4. 只增报告键纪律：不进 CI 五键门；与既有聚合函数正交
# ---------------------------------------------------------------------------


def _ci_gate_metrics() -> List[str]:
    text = CI_PATH.read_text(encoding="utf-8")
    match = re.search(r"METRICS\s*=\s*\(([^)]*)\)", text)
    assert match, "ci.yml 里找不到 METRICS 元组——写法变了请同步本测试"
    return re.findall(r'"([^"]+)"', match.group(1))


class TestReportOnlyDiscipline:
    def test_exit_keys_not_in_ci_metrics(self):
        metrics = set(_ci_gate_metrics())
        leaked = metrics & {
            "multi_agent_exit_reason_dist",
            "multi_agent_budget_exhaust_rate",
            "multi_agent_cancel_terminal_applicable",
            "multi_agent_cancel_terminal_rate",
        }
        assert not leaked, f"退出契约报告键泄入 CI 门禁：{sorted(leaked)}"

    def test_base_aggregator_returns_no_exit_keys(self):
        # p88 零接触的回归锚：老函数不认识新键
        agg = run_eval._aggregate_multi_agent(
            _rows(_meta("completed", "completed"))
        )
        assert "multi_agent_exit_reason_dist" not in agg
        assert "multi_agent_budget_exhaust_rate" not in agg
        assert "multi_agent_cancel_terminal_rate" not in agg
