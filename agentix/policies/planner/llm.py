"""LLM 规划器（F1c·A-P1-2，统一执行链 Stage F）。

零 LLM 的 ``SingleStepPlanner`` / ``DeclaredStepsPlanner`` 保持不动
（确定性 fast-path——spec 原文「DeclaredSteps 保留」）；本模块在
``AGENT_LLM_PLANNER_ENABLED=true`` 时经 PlannerPort 注入 ``AgentLoop``
（组合根 ``agent_loop_adapter._select_planner`` 装配）：

- **结构化输出**：``with_structured_output(LLMPlanOutput)``（temperature=0，
  沿任务管线 ``_llm_decompose`` 同款）；
- **按允许工具白名单过滤**：每步的 ``tool``（缺省看 ``action``）必须落在
  组合根注入的执行表键集合内，否则整步丢弃（白名单 = A-P1-2「按任务类型
  白名单」的执行表交集校验——先过滤后成计划）；
- **fail-open**：LLM 调用失败 / 输出空或全部被滤掉 ⇒ 回落
  ``SingleStepPlanner`` 语义（flag-on 也绝不因规划失败拖垮整轮）；
- **TodoWrite 快照**：组合根注入 ``todo_provider`` 时把当前清单塞进
  规划提示（恢复上下文用）；provider 故障 ⇒ 无快照照常规划。
"""

from __future__ import annotations

from typing import Any, Callable, FrozenSet, Iterable, List, Optional, Sequence

from loguru import logger
from pydantic import BaseModel, Field

from agentix.domain.agent.types import (
    ContextSnapshot,
    Plan,
    PlanStep,
    TaskSpec,
)
from agentix.policies.planner.simple import SingleStepPlanner

__all__ = [
    "LLMPlanner",
    "LLMPlanOutput",
    "PlanStepDraft",
    "default_plan_client",
]

#: 规划提示（系统段）模板：白名单与步数上限在装配期确定性注入。
_SYSTEM_TEMPLATE = """你是任务规划器：把用户目标拆成**有序执行步骤**（≤{max_steps} 步）。

规则：
1. 每步的 action（未给 tool 时按 action 解析工具名）必须属于允许工具集合：
   {tools}
   不属于该集合的步骤会在入计划前被整步丢弃。
2. id 必须唯一且非空；depends_on 只能引用同计划内已存在的 id（可成链，不可自引用）。
3. 单步即可完成时就输出一个 action=act 的步骤；不要编造集合外的工具。
4. rationale 用一句话说明该步目的（会进入执行轨迹供复盘）。
"""


class PlanStepDraft(BaseModel):
    """LLM 结构化输出中的单步草稿（进计划前统一规整）。"""

    id: str
    action: str
    tool: Optional[str] = None
    rationale: str = ""
    depends_on: List[str] = Field(default_factory=list)


class LLMPlanOutput(BaseModel):
    """LLM 规划结构化输出契约（``with_structured_output`` 目标模型）。"""

    steps: List[PlanStepDraft] = Field(default_factory=list)


def default_plan_client() -> Any:
    """规划用 LLM 客户端（temperature=0 + ``role="planner"`` 分级施压）。"""
    from agentix.application.agents.llm_factory import get_llm_client

    return get_llm_client(temperature=0.0, tags=["agent_llm_plan"], role="planner")


def _normalize_steps(
    raw: Sequence[PlanStepDraft],
    *,
    allowed: FrozenSet[str],
    max_steps: int,
) -> List[PlanStep]:
    """白名单过滤 + 去重 + 依赖裁剪 + 步数封顶（顺序保持）。"""
    kept: List[PlanStepDraft] = []
    seen_ids: set = set()
    for item in raw or ():
        if not isinstance(item, PlanStepDraft):  # pragma: no cover - 结构化输出已校验
            continue
        step_id = str(item.id or "").strip()
        action = str(item.action or "").strip()
        if not step_id or not action or step_id in seen_ids:
            continue
        tool = str(item.tool).strip() if item.tool else None
        tool = tool or None
        name = tool or action
        if allowed and name not in allowed:
            continue  # 白名单外整步丢弃
        seen_ids.add(step_id)
        kept.append(
            PlanStepDraft(
                id=step_id,
                action=action,
                tool=tool,
                rationale=str(item.rationale or ""),
                depends_on=[d for d in item.depends_on if isinstance(d, str)],
            )
        )
        if len(kept) >= max_steps:
            break
    ids = {s.id for s in kept}
    steps: List[PlanStep] = []
    for s in kept:
        deps = tuple(
            dict.fromkeys(d for d in s.depends_on if d in ids and d != s.id)
        )
        steps.append(
            PlanStep(
                id=s.id,
                action=s.action,
                tool=s.tool,
                rationale=s.rationale,
                depends_on=deps,
            )
        )
    return steps


class LLMPlanner:
    """LLM 规划器（PlannerPort 同构：``plan(task, *, context=)``）。

    :param allowed_tools: 允许的工具名集合（组合根从执行表键注入；空集合
        = 不做过滤——无法校验时不拦，与 ``build_tool_map`` 恒非空的产线
        形态共同兜底）。
    :param max_steps: 计划步数上限（组合根注入 ``MAX_ITERATIONS``，与
        AgentLoop 执行预算同源——超长计划在规划期封顶而非执行期撞墙）。
    :param todo_provider: 可选，返回当前 TodoWrite 清单快照（list[dict]）。
    :param client: 可选，注入已构造的 LLM 客户端（测试/自定义）；缺省
        每次调用 ``default_plan_client()``（可 monkeypatch 单点换桩）。
    """

    def __init__(
        self,
        *,
        allowed_tools: Iterable[str] = (),
        max_steps: int = 8,
        todo_provider: Optional[Callable[[], Any]] = None,
        client: Any = None,
    ) -> None:
        self._allowed: FrozenSet[str] = frozenset(str(t) for t in allowed_tools)
        self._max_steps = max(1, int(max_steps))
        self._todo_provider = todo_provider
        self._client = client

    def _todo_snapshot_text(self) -> str:
        if self._todo_provider is None:
            return ""
        try:
            items = self._todo_provider() or []
        except Exception as exc:  # noqa: BLE001 - 快照故障不得影响规划
            logger.debug("todo snapshot unavailable for planning: {}", exc)
            return ""
        try:
            import json

            return json.dumps(items, ensure_ascii=False)
        except Exception:  # noqa: BLE001 - 序列化失败同样不拦
            return ""

    async def _invoke(self, task: TaskSpec) -> List[PlanStep]:
        client = self._client if self._client is not None else default_plan_client()
        system = _SYSTEM_TEMPLATE.format(
            max_steps=self._max_steps,
            tools=sorted(self._allowed) or "（未提供执行表）",
        )
        import json

        user_parts = [f"目标：{task.goal}"]
        if task.constraints:
            user_parts.append(
                "约束：" + json.dumps(task.constraints, ensure_ascii=False, default=str)
            )
        todos = self._todo_snapshot_text()
        if todos:
            user_parts.append(f"当前 TodoWrite 清单：{todos}")
        output = await client.with_structured_output(LLMPlanOutput).ainvoke(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": "\n".join(user_parts)},
            ]
        )
        if not isinstance(output, LLMPlanOutput):  # pragma: no cover - 结构化契约
            raise TypeError(f"unexpected plan output type: {type(output)!r}")
        return _normalize_steps(
            output.steps, allowed=self._allowed, max_steps=self._max_steps
        )

    async def plan(
        self, task: TaskSpec, *, context: Optional[ContextSnapshot] = None
    ) -> Plan:
        try:
            steps = await self._invoke(task)
            if steps:
                return Plan(steps=tuple(steps), version=1, planner="llm")
            logger.debug("LLM planner produced no usable steps; falling back to single-step")
        except Exception as exc:  # noqa: BLE001 - fail-open 回单步语义
            logger.debug("LLM planner failed; falling back to single-step: {}", exc)
        return await SingleStepPlanner().plan(task, context=context)
