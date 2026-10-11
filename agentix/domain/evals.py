"""评估统计与成本全式（Phase 10 Evaluation 2.0）——零第三方纯函数。

书 §7.3/§7.8/§7.10 修订：
- **首错步归因**真源在 ``application.services.trajectory.locate_first_error``；
- **Pass@k vs Pass^k** 口径在 ``evals.run_eval.compute_pass_k``；
- **1–2pp 不显著**：见 :func:`is_improvement_significant`（双侧两比例 z 检验近似）；
- **成本全式**：见 :func:`llm_call_cost`（input/output/reasoning/cached + 可选固定开销）。

domain 零第三方：不 import numpy / scipy / settings。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

__all__ = [
    "LLMCostBreakdown",
    "SIGNIFICANCE_Z_95",
    "SIGNIFICANCE_Z_99",
    "aggregate_run_costs",
    "is_improvement_significant",
    "llm_call_cost",
    "two_proportion_z",
]


# ---------------------------------------------------------------------------
# 统计显著性（两比例 z 检验，双侧）
# ---------------------------------------------------------------------------


def two_proportion_z(
    successes_a: int,
    n_a: int,
    successes_b: int,
    n_b: int,
) -> Optional[float]:
    """两独立比例的双侧 z 统计量（pooled SE）。

    任一侧 ``n == 0`` 或 pooled p 为 0/1 导致 SE=0 时返回 ``None``
    （无数据 / 零方差，不是「不显著」——语义上要区分）。
    """
    if n_a <= 0 or n_b <= 0:
        return None
    if successes_a < 0 or successes_b < 0:
        raise ValueError("successes must be >= 0")
    if successes_a > n_a or successes_b > n_b:
        raise ValueError("successes cannot exceed n")
    p_a = successes_a / n_a
    p_b = successes_b / n_b
    p_pool = (successes_a + successes_b) / (n_a + n_b)
    se = math.sqrt(p_pool * (1.0 - p_pool) * (1.0 / n_a + 1.0 / n_b))
    if se == 0.0:
        return None
    return (p_a - p_b) / se


#: 双侧检验 |z| 阈值（α≈0.05 → 1.96；α≈0.01 → 2.576）
SIGNIFICANCE_Z_95 = 1.96
SIGNIFICANCE_Z_99 = 2.576


def is_improvement_significant(
    successes_baseline: int,
    n_baseline: int,
    successes_candidate: int,
    n_candidate: int,
    *,
    alpha_z: float = SIGNIFICANCE_Z_95,
    min_abs_delta_pp: float = 2.0,
) -> Dict[str, Any]:
    """候选相对基线的提升是否**统计显著**且**幅度够大**。

    书 §7.10：``1–2pp 不显著``——即使 z 过线，绝对提升 < ``min_abs_delta_pp``
    （百分点）也判 ``practically_meaningful=False``。返回结构::

        {
          "significant": bool,          # |z| >= alpha_z 且方向为提升或持平讨论
          "practically_meaningful": bool,  # significant 且 Δpp >= min_abs_delta_pp
          "delta_pp": float,            # 候选 - 基线（百分点，可为负）
          "z": float | None,
          "baseline_rate": float,
          "candidate_rate": float,
          "reason": str,
        }
    """
    if n_baseline <= 0 or n_candidate <= 0:
        return {
            "significant": False,
            "practically_meaningful": False,
            "delta_pp": 0.0,
            "z": None,
            "baseline_rate": 0.0,
            "candidate_rate": 0.0,
            "reason": "insufficient_n",
        }
    rate_b = successes_baseline / n_baseline
    rate_c = successes_candidate / n_candidate
    delta_pp = (rate_c - rate_b) * 100.0
    z = two_proportion_z(
        successes_baseline, n_baseline, successes_candidate, n_candidate
    )
    if z is None:
        return {
            "significant": False,
            "practically_meaningful": False,
            "delta_pp": round(delta_pp, 4),
            "z": None,
            "baseline_rate": round(rate_b, 6),
            "candidate_rate": round(rate_c, 6),
            "reason": "zero_variance_or_no_data",
        }
    # 双侧：只关心「候选是否优于基线」时用单侧方向解读——
    # 这里保持双侧 |z|，并单独要求 delta >= min_abs_delta_pp。
    significant = abs(z) >= alpha_z and delta_pp > 0
    practical = significant and delta_pp >= min_abs_delta_pp
    if not significant:
        reason = "not_statistically_significant"
    elif not practical:
        reason = f"delta_below_{min_abs_delta_pp:g}pp"
    else:
        reason = "ok"
    return {
        "significant": significant,
        "practically_meaningful": practical,
        "delta_pp": round(delta_pp, 4),
        "z": round(z, 4),
        "baseline_rate": round(rate_b, 6),
        "candidate_rate": round(rate_c, 6),
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# 成本全式（Phase 10）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMCostBreakdown:
    """单次 LLM 调用的成本拆解（货币单位由部署方单价决定）。

    全式::

        cost = (input * p_in + output * p_out + reasoning * p_reason
                + cached * p_cached) / 1_000_000
               + fixed_overhead
    """

    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    price_input_per_1m: float = 0.0
    price_output_per_1m: float = 0.0
    price_reasoning_per_1m: float = 0.0
    price_cached_per_1m: float = 0.0
    fixed_overhead: float = 0.0

    def breakdown(self) -> Dict[str, float]:
        m = 1_000_000.0
        parts = {
            "input": self.input_tokens * self.price_input_per_1m / m,
            "output": self.output_tokens * self.price_output_per_1m / m,
            "reasoning": self.reasoning_tokens * self.price_reasoning_per_1m / m,
            "cached": self.cached_tokens * self.price_cached_per_1m / m,
            "overhead": float(self.fixed_overhead),
        }
        return {k: round(v, 8) for k, v in parts.items()}

    @property
    def total(self) -> float:
        return round(sum(self.breakdown().values()), 8)

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.reasoning_tokens
            + self.cached_tokens
        )


def llm_call_cost(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    reasoning_tokens: int = 0,
    cached_tokens: int = 0,
    price_input_per_1m: float = 0.0,
    price_output_per_1m: float = 0.0,
    price_reasoning_per_1m: float = 0.0,
    price_cached_per_1m: float = 0.0,
    fixed_overhead: float = 0.0,
) -> LLMCostBreakdown:
    """构造并返回成本拆解（默认单价 0.0 → 诚实 0.0，不假装有价格）。"""
    for name, n in (
        ("input_tokens", input_tokens),
        ("output_tokens", output_tokens),
        ("reasoning_tokens", reasoning_tokens),
        ("cached_tokens", cached_tokens),
    ):
        if n < 0:
            raise ValueError(f"{name} must be >= 0")
    return LLMCostBreakdown(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cached_tokens=cached_tokens,
        price_input_per_1m=price_input_per_1m,
        price_output_per_1m=price_output_per_1m,
        price_reasoning_per_1m=price_reasoning_per_1m,
        price_cached_per_1m=price_cached_per_1m,
        fixed_overhead=fixed_overhead,
    )


def aggregate_run_costs(breakdowns: Sequence[LLMCostBreakdown]) -> Dict[str, Any]:
    """多次调用/多任务成本聚合（评测报告用）。"""
    if not breakdowns:
        return {"calls": 0, "total_cost": 0.0, "total_tokens": 0, "mean_cost": 0.0}
    total = sum(b.total for b in breakdowns)
    tokens = sum(b.total_tokens for b in breakdowns)
    return {
        "calls": len(breakdowns),
        "total_cost": round(total, 8),
        "total_tokens": tokens,
        "mean_cost": round(total / len(breakdowns), 8),
    }
