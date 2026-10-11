"""内置 Planner（Phase 2 起）：把 goal 拆成单步或按 meta.steps 声明展开。

零 LLM：确定性，便于单测与离线回归。后续 LLM Planner 可经 PlannerPort 注入。
"""

from __future__ import annotations

from typing import Optional

from agentix.domain.agent.types import ContextSnapshot, Plan, PlanStep, TaskSpec

__all__ = ["SingleStepPlanner", "DeclaredStepsPlanner"]


class SingleStepPlanner:
    """最简 Planner：始终产出一个 ``act`` 步（Harness 冒烟用）。"""

    async def plan(self, task: TaskSpec, *, context: Optional[ContextSnapshot] = None) -> Plan:
        return Plan(
            steps=(PlanStep(id="s1", action="act", rationale=task.goal[:200]),),
            version=1,
            planner="single_step",
        )


class DeclaredStepsPlanner:
    """从 ``task.meta``/``constraints['steps']`` 读取声明式步骤（无则单步）。

    约束里 ``steps`` 形如 ``[{"id": "a", "action": "search", "depends_on": []}, ...]``。
    """

    async def plan(self, task: TaskSpec, *, context: Optional[ContextSnapshot] = None) -> Plan:
        raw = task.constraints.get("steps") if task.constraints else None
        if not raw or not isinstance(raw, (list, tuple)):
            return await SingleStepPlanner().plan(task, context=context)
        steps = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            steps.append(
                PlanStep(
                    id=str(item.get("id", f"s{i+1}")),
                    action=str(item.get("action", "act")),
                    depends_on=tuple(item.get("depends_on") or ()),
                    tool=item.get("tool"),
                    rationale=str(item.get("rationale", "")),
                )
            )
        if not steps:
            return await SingleStepPlanner().plan(task, context=context)
        return Plan(steps=tuple(steps), version=1, planner="declared")
