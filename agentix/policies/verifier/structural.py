"""结构层 Verifier（L1）：不调 LLM，只检查观测完整性与 task 成功条件。

Phase 7 将叠加规则层与 grounding 层；此处为骨架。
"""

from __future__ import annotations

from typing import Sequence

from agentix.domain.agent.types import (
    FailureLayer,
    Observation,
    Plan,
    TaskSpec,
    VerificationResult,
)

__all__ = ["StructuralVerifier"]


class StructuralVerifier:
    """Proposer–Reviewer：只看 observation 结构与 success_criteria 命中。"""

    async def verify(
        self,
        task: TaskSpec,
        plan: Plan,
        observations: Sequence[Observation],
    ) -> VerificationResult:
        if not observations:
            return VerificationResult(
                passed=False,
                reason="no_observations",
                failure_layer=FailureLayer.CONTROL_FLOW,
            )
        failed = [o for o in observations if not o.ok]
        if failed and len(failed) == len(observations):
            return VerificationResult(
                passed=False,
                reason="all_observations_failed",
                failure_layer=failed[0].failure_layer or FailureLayer.TOOL,
            )
        blob = "\n".join(o.content for o in observations)
        missing = [c for c in task.success_criteria if c and c not in blob]
        if missing:
            return VerificationResult(
                passed=False,
                reason=f"missing_criteria:{missing[:3]}",
            )
        ok_ratio = (len(observations) - len(failed)) / max(1, len(observations))
        return VerificationResult(
            passed=True,
            reason="structural_ok",
            score=ok_ratio,
        )
