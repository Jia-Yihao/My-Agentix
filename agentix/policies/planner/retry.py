"""Replanner（Phase 3）：失败后产出**新** Plan（Append-only：不改旧 Plan）。

策略：
- ``max_replans``：总重规划上限（默认 3）；
- ``same_failure_count``：连续同一 ``failure_key`` 达到阈值即熔断（不再重规划）。

P4-4.1：``guardrails`` 可选注入时，重规划产出的重试步 rationale 会带上该
工具的**恢复建议**（回喂通道之二；通道之一是 ``task_retry`` 终局追加）。
``None``（默认）= rationale 与基线逐位等价。
"""

from __future__ import annotations

from typing import Optional, Sequence

from agentix.domain.agent.types import (
    ContextSnapshot,
    Observation,
    Plan,
    PlanStep,
    TaskSpec,
)

__all__ = ["RetryReplanner", "failure_key"]


def failure_key(obs: Observation) -> str:
    """故障指纹：error_code + content 前缀（用于 same-failure 计数）。"""
    return f"{obs.error_code or 'fail'}|{obs.content[:80]}"


class RetryReplanner:
    """最简纠偏：把失败步的 tool/action 重新插入为新 Plan 的唯一步。

    若 ``fallback_action`` 提供，则优先改用 fallback（工具降级路径）。
    """

    def __init__(
        self,
        *,
        max_replans: int = 3,
        same_failure_threshold: int = 2,
        fallback_action: Optional[str] = None,
        planner=None,
        guardrails=None,
    ) -> None:
        self.max_replans = max(0, max_replans)
        self.same_failure_threshold = max(1, same_failure_threshold)
        self._fallback_action = fallback_action
        self._planner = planner
        #: P4-4.1：``ToolGuardrails``（可选）。提供时重试步 rationale 带恢复建议。
        self._guardrails = guardrails
        self._same_failure_streak = 0
        self._last_key: Optional[str] = None

    def _advice_rationale(self, tool: Optional[str], base: str) -> str:
        """把工具专属恢复建议拼进 rationale（无建议/未注入 ⇒ 原文）。"""
        if self._guardrails is None or not tool:
            return base
        try:
            advice = self._guardrails.advice_for(tool)
        except Exception:  # noqa: BLE001 - 护栏故障不得影响重规划
            return base
        if not advice:
            return base
        return f"{base}；恢复建议：{advice}"

    def reset(self) -> None:
        self._same_failure_streak = 0
        self._last_key = None

    def should_replan(self, last_failure: Observation) -> bool:
        """是否还允许基于该失败重规划（连续同因熔断）。"""
        key = failure_key(last_failure)
        if key == self._last_key:
            self._same_failure_streak += 1
        else:
            self._last_key = key
            self._same_failure_streak = 1
        return self._same_failure_streak < self.same_failure_threshold

    async def replan(
        self,
        task: TaskSpec,
        plan: Plan,
        context: ContextSnapshot,
        observations: Sequence[Observation],
        *,
        last_failure: Observation,
    ) -> Optional[Plan]:
        if not self.should_replan(last_failure):
            return None
        failed_step = plan.step_by_id(last_failure.step_id)
        action = failed_step.action if failed_step else "retry"
        tool = failed_step.tool if failed_step else None
        if self._fallback_action:
            action = self._fallback_action
            tool = self._fallback_action
        # 新 Plan：version+1，只含失败步的重试（调用方 may clear done ids 或
        # 用新 step id 避免被 _next_pending 跳过）
        retry_id = f"r{plan.version}_{last_failure.step_id}"
        new_step = PlanStep(
            id=retry_id,
            action=action,
            tool=tool,
            rationale=self._advice_rationale(
                tool, f"replan after {last_failure.error_code or 'fail'}"
            ),
        )
        if self._planner is not None:
            try:
                custom = await self._planner.plan(task, context=context)
                if custom.steps:
                    done_ids = {o.step_id for o in observations}
                    remapped: list[PlanStep] = []
                    seen: set[str] = set()
                    for i, s in enumerate(custom.steps):
                        sid = s.id
                        if sid in done_ids or sid in seen:
                            sid = f"r{plan.version}_{s.id}_{i}"
                        seen.add(sid)
                        remapped.append(
                            PlanStep(
                                id=sid,
                                action=s.action,
                                tool=s.tool,
                                rationale=s.rationale,
                                depends_on=tuple(
                                    d for d in s.depends_on if d not in done_ids
                                ),
                            )
                        )
                    return Plan(
                        steps=tuple(remapped),
                        version=plan.version + 1,
                        planner=custom.planner,
                    )
            except Exception:  # noqa: BLE001
                pass
        return Plan(steps=(new_step,), version=plan.version + 1, planner="retry")
