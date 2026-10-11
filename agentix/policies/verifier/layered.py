"""分层 Verifier（Phase 7）：L1 结构 / L2 规则 / L3 grounding。

Proposer–Reviewer 隔离：只消费 Observation.content / task.success_criteria /
证据池；**不读** Proposer 自述字段（plan.rationale、meta 中的 narrative 等）。

L3 grounding 复用 retrieval 的纯函数 ``validate_answer_grounding``（零 LLM）。
"""

from __future__ import annotations

from typing import Optional, Sequence

from agentix.policies.verifier.structural import StructuralVerifier
from agentix.domain.agent.types import (
    FailureLayer,
    Observation,
    Plan,
    TaskSpec,
    VerificationResult,
)
from agentix.domain.policies.completion import (
    CompletionSignal,
    validate_completion,
)

__all__ = ["LayeredVerifier"]


class LayeredVerifier:
    """L1 结构 → L2 完成度规则 → L3 grounding，按层短路。

    - L1 失败 → 直接 fail（结构性不可通过）；
    - L2 INCOMPLETE → fail；DEGRADED → 通过但记降级；
    - L3 仅在有证据且有实质答案时执行；score < threshold → fail。
    """

    def __init__(
        self,
        *,
        require_grounding: bool = False,
        grounding_threshold: float = 0.35,
        route: Optional[str] = None,
    ) -> None:
        self._structural = StructuralVerifier()
        self._require_grounding = require_grounding
        self._threshold = grounding_threshold
        self._route = route

    async def verify(
        self,
        task: TaskSpec,
        plan: Plan,
        observations: Sequence[Observation],
    ) -> VerificationResult:
        # ---- Proposer–Reviewer：剥离自述面 ----
        # 只看 content/ok/failure_layer；plan.rationale 不进入判定。

        # L1 structural
        l1 = await self._structural.verify(task, plan, observations)
        if not l1.passed:
            return l1

        # 提取答案与证据（最后一个成功观测 = 最终产物面）
        successes = [o for o in observations if o.ok]
        answer = successes[-1].content if successes else ""
        failures = [o for o in observations if not o.ok]
        evidence_pool = self._extract_evidence(observations)

        # L2 completion rules（domain.policies.completion 纯函数）
        # 注意：失败观测的 content 是错误描述，**不**当 errors 传入
        # （否则任何一次工具失败都会被当成「未消解 error」→ INCOMPLETE）；
        # 失败计数走 tool_failures，真正的 error 字符串走 error_code。
        unresolved_errors = [
            o.error_code or ""
            for o in failures
            if o.error_code and o.error_code not in ("tool_exec_failed",)
        ]
        verdict = validate_completion(
            route=self._route,
            answer=answer,
            sources=evidence_pool if evidence_pool else None,
            errors=unresolved_errors,
            tool_failures=len(failures),
        )
        if verdict.signal is CompletionSignal.INCOMPLETE:
            return VerificationResult(
                passed=False,
                reason=f"completion_incomplete:{verdict.reasons[:2]}",
                failure_layer=FailureLayer.CONTROL_FLOW,
                meta={"l2": verdict.to_dict()},
            )

        meta: dict = {"l1": l1.reason, "l2": verdict.to_dict()}

        # L3 grounding（可选；零 LLM 纯函数；无证据 fail-open 并记录 skipped）
        if self._require_grounding:
            from agentix.domain.policies.grounding_verify import (
                validate_answer_grounding,
            )

            report = validate_answer_grounding(
                answer, evidence_pool, support_threshold=self._threshold
            )
            meta["l3"] = report.to_dict()
            if (
                report.skipped_reason is None
                and report.score is not None
                and report.score < self._threshold
            ):
                return VerificationResult(
                    passed=False,
                    reason=f"grounding_below_threshold:{report.score}",
                    failure_layer=FailureLayer.TOOL,
                    meta=meta,
                )

        degraded = verdict.signal is CompletionSignal.DEGRADED
        return VerificationResult(
            passed=True,
            reason="layered_ok_degraded" if degraded else "layered_ok",
            score=l1.score,
            meta=meta,
        )

    @staticmethod
    def _extract_evidence(observations: Sequence[Observation]) -> list[dict]:
        """从观测 meta/artifacts 抽取证据块（content/source 字段约定）。"""
        pool: list[dict] = []
        for o in observations:
            if not o.ok:
                continue
            blocks = o.meta.get("evidence") or o.meta.get("hit_chunks") or []
            if isinstance(blocks, list):
                for b in blocks:
                    if isinstance(b, dict) and (
                        b.get("content") or b.get("page_content") or b.get("snippet")
                    ):
                        pool.append(b)
            for art in o.artifacts:
                if isinstance(art, str) and art.startswith("{"):
                    import json

                    try:
                        b = json.loads(art)
                        if isinstance(b, dict) and b.get("content"):
                            pool.append(b)
                    except Exception:  # noqa: BLE001
                        continue
        return pool
