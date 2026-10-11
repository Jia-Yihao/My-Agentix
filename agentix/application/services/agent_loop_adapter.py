"""Phase 15：AgentLoop（L3 harness）↔ 生产 ask 支路适配层。

职责（规划 §3.3 15.4）：
1. **TaskSpec 构建** —— 用户消息 → domain 开放任务；
2. **工具桥** —— ``MappingStepExecutor`` 的 ``fn(task=, step=, context=)``
   契约；默认注册 ``act`` / ``final_answer``（SingleStepPlanner 单步冒烟），
   可注入真实工具 callable；
3. **LoopResult → 图结果 dict** —— 对齐 ``chat_service`` 下游消费的
   ``messages`` / ``router`` / ``sources`` 形状；
4. **轨迹桥** —— ``LoopResult.observations`` → ``TrajectoryRecorder`` + ``record_tool``。

约定：
- **stream 不经本模块**（决策 A：AgentLoop 无流式 API）；
- 本模块不 import LangGraph；
- 轨迹写入自吞异常，绝不影响主链路。
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from loguru import logger

from agentix.agent.context import (
    SimpleContextBuilder,
    build_instructions,
    mentioned_instruction_dirs,
)
from agentix.orchestration.orchestrator import AgentLoop, LoopResult
from agentix.orchestration.executor import MappingStepExecutor, ToolCallable
from agentix.policies.planner import SingleStepPlanner
from agentix.policies.verifier import LayeredVerifier
from agentix.config import settings
from agentix.domain.agent.types import ContextSnapshot, TaskSpec
from agentix.domain.tracing import get_trace_id
from agentix.application.services.budget_notice import budget_notice_observer

__all__ = [
    "build_task_spec",
    "build_tool_map",
    "map_loop_result",
    "bridge_observations_to_trajectory",
    "run_agent_loop_ask",
]


def _maybe_compactor() -> Optional[Any]:
    """L4 压缩器组合根装配（Phase 22）：flag 默认关 ⇒ None（零行为变化）。"""
    if not getattr(settings, "CONTEXT_AUTOCOMPACT", False):
        return None
    try:
        from agentix.application.agents.compactor import LLMCompactor

        return LLMCompactor()
    except Exception as exc:  # noqa: BLE001 - 装配失败 ⇒ L4 降级关闭
        logger.debug("LLMCompactor unavailable; L4 disabled: {}", exc)
        return None


def _budget_status_bar() -> Optional[Dict[str, Any]]:
    """Phase 28（Ch10 §10.6）：状态栏预算键（构造侧 flag-gated 写入）。

    - flag 关（默认）⇒ ``None`` ⇒ SimpleContextBuilder 状态栏为空（与基线字节等价）；
    - flag 开 ⇒ 仅写 **常量** ``max_iter``（同配置前缀稳定，KV Cache 纪律）；
      动态剩余量走轨迹 ``budget_notice``（``budget_notice.py``），拒绝进状态栏。
    """
    if not settings.BUDGET_NOTICE_ENABLED:
        return None
    return {"max_iter": str(int(settings.MAX_ITERATIONS))}


class _LoopMessage:
    """最小消息载体：满足 ``.content`` / ``.additional_kwargs`` / ``.tool_calls``
    读取约定（``chat_service`` 非流式路径与 ``_extract_tool_names``）。"""

    __slots__ = ("content", "additional_kwargs", "tool_calls", "type")

    def __init__(
        self,
        content: str,
        *,
        degraded: bool = False,
        tool_calls: Optional[list] = None,
    ) -> None:
        self.content = content
        self.additional_kwargs: Dict[str, Any] = {"degraded": degraded}
        self.tool_calls = tool_calls or []
        self.type = "ai"


def build_task_spec(
    message: str,
    *,
    session_id: str,
    constraints: Optional[Dict[str, Any]] = None,
    success_criteria: tuple = (),
) -> TaskSpec:
    """用户消息 → Agent Core 开放任务。"""
    task_id = f"{session_id}:{get_trace_id()}" if session_id else get_trace_id()
    return TaskSpec(
        goal=message,
        task_id=task_id,
        constraints=dict(constraints or {}),
        success_criteria=tuple(success_criteria),
    )


def build_tool_map(
    extra: Optional[Mapping[str, ToolCallable]] = None,
) -> Dict[str, ToolCallable]:
    """默认工具表 + 可注入扩展（真实 KG/KB callable 由上层注册）。

    ``SingleStepPlanner`` 产出 ``action="act"``；映射到可产出终态文本的
    ``act`` 工具，保证 flag-on 冒烟可走到 verify 终态。
    """

    def _act(*, task: TaskSpec, step, context: ContextSnapshot) -> str:
        # Harness 级最小回执：不伪造检索结果；真实工具经 extra 注入。
        return task.goal

    def _final_answer(*, task: TaskSpec, step, context: ContextSnapshot) -> str:
        return task.goal

    tools: Dict[str, ToolCallable] = {
        "act": _act,
        "final_answer": _final_answer,
    }
    if extra:
        tools.update(dict(extra))
    return tools


def map_loop_result(
    result: LoopResult,
    *,
    route: Optional[str] = None,
    route_logic: Optional[str] = None,
) -> Dict[str, Any]:
    """``LoopResult`` → ``chat_service`` 下游期望的图结果 dict。"""
    degraded = not result.passed
    message = _LoopMessage(
        content=result.final or "",
        degraded=degraded,
        tool_calls=[
            {"name": str(o.meta.get("tool") or o.step_id or "step"), "args": {}}
            for o in result.observations
            if o.ok and o.content
        ],
    )
    logic = route_logic or f"agent_loop:{result.terminal}"
    if result.reason:
        logic = f"{logic}:{result.reason[:120]}"
    return {
        "messages": [message],
        "router": {"type": route, "logic": logic},
        "sources": [],
        "degraded": degraded,
        "agent_loop": {
            "terminal": result.terminal,
            "steps_used": result.steps_used,
            "replans": result.replans,
            "task_id": result.task_id,
        },
    }


def bridge_observations_to_trajectory(result: LoopResult) -> None:
    """observations → services 轨迹 + request_scope tools_used（自吞异常）。"""
    try:
        from agentix.application.services.request_scope import record_tool
        from agentix.application.services.trajectory import get_trajectory_recorder
    except Exception as exc:  # noqa: BLE001
        logger.debug("trajectory bridge skipped: {}", exc)
        return

    recorder = get_trajectory_recorder()
    for obs in result.observations:
        status = "ok" if obs.ok else "error"
        name = str(obs.meta.get("tool") or obs.step_id or "step")
        recorder.record(
            kind="step",
            name=name,
            status=status,
            detail={
                "step_id": obs.step_id,
                "terminal_hint": result.terminal,
                "failure_layer": obs.failure_layer.value if obs.failure_layer else None,
            },
            error=None if obs.ok else (obs.error_code or obs.content[:200]),
        )
        if obs.ok:
            try:
                record_tool(name)
            except Exception:  # noqa: BLE001
                pass


def _todo_snapshot() -> list:
    """F1c·A-P1-2：TodoWrite 清单快照（进 LLM 规划提示；故障 ⇒ 空 fail-open）。"""
    try:
        from agentix.application.services.todo_write import get_todo_store

        return list(get_todo_store().snapshot().get("items") or [])
    except Exception:  # noqa: BLE001 - 快照故障不得影响规划
        return []


def _select_planner(tools: Mapping[str, ToolCallable]) -> Any:
    """F1c·A-P1-2：planner 选择。

    flag 关（默认）⇒ ``SingleStepPlanner``（与基线同型同参，逐位等价）；
    flag 开 ⇒ ``LLMPlanner``（结构化输出 + 执行表白名单 + Todo 快照注入）。
    """
    if not bool(getattr(settings, "AGENT_LLM_PLANNER_ENABLED", False)):
        return SingleStepPlanner()
    from agentix.policies.planner.llm import LLMPlanner

    return LLMPlanner(
        allowed_tools=frozenset(tools),
        max_steps=max(1, int(settings.MAX_ITERATIONS)),
        todo_provider=_todo_snapshot,
    )


def _select_replanner(max_replans: int, *, planner: Any = None) -> Any:
    """RetryReplanner 装配（P1·``AGENT_MAX_REPLANS``；F1c 起 flag-on 时把
    LLM planner 注入为内层重规划器）。

    ``max_replans<=0`` ⇒ ``None``（不接 replanner，与基线逐位等价）；
    ``planner=None`` ⇒ 不传 ``planner`` kwarg（RetryReplanner 内层保持
    None，重规划走原单步插入语义——flag-off 分支逐位等价）。
    """
    if max_replans <= 0:
        return None
    from agentix.policies.planner import RetryReplanner

    # P4-4.1：护栏 flag 开时注入 guardrails —— 重规划步 rationale 带
    # 工具专属恢复建议（flag 关 ⇒ None ⇒ rationale 与基线逐位等价）。
    guardrails = None
    if bool(getattr(settings, "TOOL_GUARDRAILS_ENABLED", False)):
        from agentix.policies.guardrails.guardrails import get_tool_guardrails

        guardrails = get_tool_guardrails()
    if planner is not None:
        return RetryReplanner(
            max_replans=max_replans, guardrails=guardrails, planner=planner
        )
    return RetryReplanner(max_replans=max_replans, guardrails=guardrails)


async def run_agent_loop_ask(
    message: str,
    session_id: str,
    *,
    route: Optional[str] = None,
    extra_tools: Optional[Mapping[str, ToolCallable]] = None,
) -> Dict[str, Any]:
    """在 ``AGENT_LOOP_ENABLED=True`` 时替代 ``graph.ainvoke`` 的 ask 支路。

    调用方须已持有 ``_AGENT_SEM`` / ``AGENT_TIMEOUT`` / ``runtime.astep`` 包裹。
    """
    task = build_task_spec(message, session_id=session_id)
    tools = build_tool_map(extra_tools)
    # P1（任务执行）：AGENT_MAX_REPLANS>0 时接入 RetryReplanner（失败步后
    # 重规划，连续同因由 same_failure_threshold 熔断）；=0（默认）不接
    # replanner、不传 max_replans —— 与基线逐位等价。
    max_replans = int(getattr(settings, "AGENT_MAX_REPLANS", 0) or 0)
    # F1c·A-P1-2：flag-off ⇒ SingleStepPlanner 与基线逐位等价；flag-on ⇒
    # LLMPlanner 并注入 RetryReplanner 内层（若接了 replanner）。
    planner = _select_planner(tools)
    replanner = _select_replanner(
        max_replans,
        planner=planner
        if bool(getattr(settings, "AGENT_LLM_PLANNER_ENABLED", False))
        else None,
    )
    loop = AgentLoop(
        planner=planner,
        context=SimpleContextBuilder(
            status_bar=_budget_status_bar(),
            # F1b2·M-P1-4（AGENTS 级联）：消息触达的子目录才追加指令层；
            # flag-off（默认）mentioned_instruction_dirs 返回 () ⇒
            # build_instructions 输出与基线逐字节一致。
            rules=build_instructions(
                subdirs=mentioned_instruction_dirs(message)
            ),
            compactor=_maybe_compactor(),
        ),
        executor=MappingStepExecutor(tools=tools),
        # Stage F2b-d（R-P2-3）：L3 grounding 总闸——默认关=基线
        # require_grounding=False 逐位等价
        verifier=LayeredVerifier(
            require_grounding=bool(
                getattr(settings, "VERIFIER_GROUNDING_ENABLED", False)
            ),
            route=route,
        ),
        max_steps=max(1, int(settings.MAX_ITERATIONS)),
        max_replans=max_replans,
        replanner=replanner,
        budget_observer=(
            budget_notice_observer if settings.BUDGET_NOTICE_ENABLED else None
        ),
        # P3（渐进上下文）：>0 = 每步按累计观测重建上下文并按预算裁剪；
        # 0（默认）= 一次性构建，与基线逐位等价（值在组合根读 settings，
        # AgentLoop 保持不 import config 的纯编排约束）。
        context_budget_tokens=int(
            getattr(settings, "AGENT_CONTEXT_BUDGET_TOKENS", 0) or 0
        ),
        # F1d·A-P1-3（并发工具轮）：1（默认）= 单步串行与基线逐位等价；
        # >1 = ready 批并发（授权门 + 危险强制串行，组合根注入）。
        round_concurrency=int(settings.AGENT_TOOL_ROUND_CONCURRENCY),
    )
    result = await loop.run(task)
    bridge_observations_to_trajectory(result)
    return map_loop_result(result, route=route)
