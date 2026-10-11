"""P1 ``execute_task`` 图节点（任务型路由的落点，flag ``TASK_EXEC_ENABLED`` 门控）。

节点**无条件注册**进扁平图（phase19 绊线断言 ``route_query`` 的 Literal、
``ROUTE_HANDLERS``、图节点集三者一致），可达性由路由侧 flag 门控——
flag-off 时 ``route_query`` 不会把任何请求送到这里，行为与基线等价；
本函数首行再做一次防御性降级（belt-and-suspenders，同 notify_bridge 风格）。

职责链：取 message/session → ``run_task_execution``（拆解→波次→治理执行→
分层校验）→ 轨迹桥 → ``map_loop_result`` 对齐 chat_service 下游形状 →
组装 ``AIMessage``（含逐步 tool_calls 轨迹）+ ``TaskRouter`` 状态回写。

流式：本节点无 chat-model token 事件，``chat_service`` 整段兜底
（``_extract_final_text``）会把最终回执整段发出——无需进
``_STREAM_FORWARD_NODES``（该白名单只转发回答类节点的 LLM token）。
"""

from __future__ import annotations

from typing import Any, Dict

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from loguru import logger

from agentix.orchestration.langgraph.lg_states import AgentState, Router, TaskRouter
from agentix.application.agents.router import _extract_configurable
from agentix.config import settings

__all__ = ["execute_task"]


def _message_text(message: Any) -> str:
    """末条用户消息 → 纯文本（多模态 part 列表时拼接 text 部分）。"""
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts = []
        for item in message:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return str(message or "")


async def execute_task(state: AgentState, *, config: RunnableConfig) -> Dict[str, Any]:
    """任务执行节点：run_task_execution → 图状态更新（messages/router/sources）。"""
    if not settings.TASK_EXEC_ENABLED:
        # 防御性降级：正常链路不可达（路由侧已门控）；返回 {} = 零状态变更。
        logger.warning("execute_task invoked while TASK_EXEC_ENABLED=false; no-op")
        return {}

    message = _message_text(state.messages[-1].content if state.messages else "")
    cfg = _extract_configurable(config)
    session_id = str(cfg.get("thread_id") or "")
    task_steps = cfg.get("task_steps")
    # F1e·A-P1-4：进环中断回调（lg_builder 注入壳塞进 configurable；flag-off /
    # py3.10 / stream 未声明进环 ⇒ 缺省 = 现行为 review_required 快速失败）。
    review_interrupt = cfg.get("hitl_in_loop_interrupt")

    from agentix.application.services.agent_loop_adapter import (
        bridge_observations_to_trajectory,
        map_loop_result,
    )
    from agentix.application.services.task_execution import run_task_execution

    result = await run_task_execution(
        message,
        session_id=session_id,
        task_steps=task_steps,
        route="task-query",
        review_interrupt=review_interrupt if callable(review_interrupt) else None,
    )
    bridge_observations_to_trajectory(result)

    mapped = map_loop_result(
        result,
        route="task-query",
        route_logic=f"task_exec:{result.terminal}",
    )
    src = mapped["messages"][0]
    tool_calls = [
        {**tc, "id": f"task_step_{idx + 1}", "type": "tool_call"}
        for idx, tc in enumerate(getattr(src, "tool_calls", None) or [])
    ]
    ai_message = AIMessage(
        content=src.content,
        tool_calls=tool_calls,
        additional_kwargs={"degraded": bool(mapped.get("degraded"))},
    )

    prev = state.router if isinstance(state.router, Router) else None
    router = TaskRouter(
        type="task-query",
        logic=mapped["router"]["logic"],
        question=(prev.question if prev else "") or message,
        decision=prev.decision if prev else None,
        confidence=prev.confidence if prev else None,
        reasoning=prev.reasoning if prev else None,
    )

    logger.info(
        "execute_task done: terminal={} steps_used={} passed={}",
        result.terminal,
        result.steps_used,
        result.passed,
    )
    return {
        "messages": [ai_message],
        "router": router,
        "sources": [],
    }
