"""Phase 10 — Evaluation 2.0：显著性、成本全式、trajectory 收敛桥。"""

from __future__ import annotations

import pytest

from agentix.application.services.trajectory import TrajectoryStep, locate_first_error
from agentix.domain.evals import (
    SIGNIFICANCE_Z_95,
    aggregate_run_costs,
    is_improvement_significant,
    llm_call_cost,
    two_proportion_z,
)
from agentix.observability.trajectory import TrajectoryRecorder


class TestTwoProportionZ:
    def test_identical_rates_z_is_zero(self):
        z = two_proportion_z(50, 100, 50, 100)
        assert z == pytest.approx(0.0)

    def test_clear_improvement_large_abs_z(self):
        # a=baseline(40%), b=candidate(70%) → z = (p_a-p_b)/se < 0；
        # is_improvement_significant 用 |z| 判显著 + delta_pp>0 判方向。
        z = two_proportion_z(40, 100, 70, 100)
        assert z is not None and abs(z) > SIGNIFICANCE_Z_95
        assert z < 0  # 候选优于基线

    def test_zero_n_returns_none(self):
        assert two_proportion_z(0, 0, 10, 100) is None
        assert two_proportion_z(10, 100, 0, 0) is None

    def test_zero_variance_returns_none(self):
        # 全成功 vs 全成功 → pooled p=1 → se=0
        assert two_proportion_z(100, 100, 100, 100) is None

    def test_successes_exceed_n_raises(self):
        with pytest.raises(ValueError):
            two_proportion_z(101, 100, 10, 100)


class TestSignificance:
    def test_large_improvement_is_meaningful(self):
        r = is_improvement_significant(50, 100, 80, 100)
        assert r["significant"] is True
        assert r["practically_meaningful"] is True
        assert r["delta_pp"] == pytest.approx(30.0)
        assert r["reason"] == "ok"

    def test_small_delta_below_threshold_not_practical(self):
        # 1pp 提升：可能 z 不够或 delta < 2pp
        r = is_improvement_significant(100, 1000, 101, 1000)
        assert r["practically_meaningful"] is False

    def test_two_pp_rule_enforced(self):
        # 构造恰好 2pp 边界：100/5000 vs 200/5000 → 2pp
        r = is_improvement_significant(100, 5000, 110, 5000, min_abs_delta_pp=2.0)
        assert r["delta_pp"] == pytest.approx(0.2, abs=0.01)  # 0.2pp < 2pp
        assert r["practically_meaningful"] is False

    def test_regression_not_significant_as_improvement(self):
        r = is_improvement_significant(80, 100, 50, 100)
        assert r["significant"] is False
        assert r["delta_pp"] < 0

    def test_insufficient_n(self):
        r = is_improvement_significant(0, 0, 1, 1)
        assert r["reason"] == "insufficient_n"
        assert r["significant"] is False

    def test_zero_variance_reason(self):
        r = is_improvement_significant(100, 100, 100, 100)
        assert r["reason"] == "zero_variance_or_no_data"
        assert r["z"] is None


class TestCostFullFormula:
    def test_zero_prices_honest_zero(self):
        c = llm_call_cost(input_tokens=1000, output_tokens=500)
        assert c.total == 0.0
        assert c.total_tokens == 1500

    def test_full_formula_components(self):
        c = llm_call_cost(
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            reasoning_tokens=1_000_000,
            cached_tokens=1_000_000,
            price_input_per_1m=2.0,
            price_output_per_1m=8.0,
            price_reasoning_per_1m=1.0,
            price_cached_per_1m=0.5,
            fixed_overhead=0.1,
        )
        parts = c.breakdown()
        assert parts["input"] == pytest.approx(2.0)
        assert parts["output"] == pytest.approx(8.0)
        assert parts["reasoning"] == pytest.approx(1.0)
        assert parts["cached"] == pytest.approx(0.5)
        assert parts["overhead"] == pytest.approx(0.1)
        assert c.total == pytest.approx(11.6)

    def test_negative_tokens_rejected(self):
        with pytest.raises(ValueError):
            llm_call_cost(input_tokens=-1)

    def test_aggregate(self):
        a = llm_call_cost(input_tokens=1_000_000, price_input_per_1m=1.0)
        b = llm_call_cost(input_tokens=1_000_000, price_input_per_1m=1.0)
        agg = aggregate_run_costs([a, b])
        assert agg["calls"] == 2
        assert agg["total_cost"] == pytest.approx(2.0)
        assert agg["mean_cost"] == pytest.approx(1.0)

    def test_aggregate_empty(self):
        agg = aggregate_run_costs([])
        assert agg["calls"] == 0
        assert agg["total_cost"] == 0.0


class TestTrajectoryConvergenceBridge:
    def test_observability_first_error_matches_services_semantics(self):
        rec = TrajectoryRecorder(name="t")
        s1 = rec.begin_step("router")
        rec.end_step(s1, status="ok")
        s2 = rec.begin_step("milvus")
        rec.fail_step(s2, attributes={"error": "conn"})
        s3 = rec.begin_step("finalize")
        rec.fail_step(s3, attributes={"error": "later"})
        first = rec.first_error_step()
        assert first is not None
        assert first["name"] == "milvus"
        assert rec.failure_stage == "milvus"

    def test_as_service_steps_roundtrip_through_locate_first_error(self):
        rec = TrajectoryRecorder(name="t")
        a = rec.begin_step("ok_step")
        rec.end_step(a, status="ok")
        b = rec.begin_step("bad_step")
        rec.end_step(b, status="error", attributes={"error": "boom"})
        dicts = rec.as_service_steps()
        steps = [TrajectoryStep.from_dict(d) for d in dicts]
        fe = locate_first_error(steps)
        assert fe is not None
        assert fe.name == "bad_step"
        assert fe.step_index == 1

    def test_doc_declares_services_as_source_of_truth(self):
        import agentix.observability.trajectory as mod

        assert "services.trajectory" in (mod.__doc__ or "")
        assert "真源" in (mod.__doc__ or "")
