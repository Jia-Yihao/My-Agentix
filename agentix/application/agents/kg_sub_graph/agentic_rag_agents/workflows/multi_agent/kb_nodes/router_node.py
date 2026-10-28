"""KB router 节点（P1-1 拆分，从 multi_tool.py 搬移）。

工厂注入依赖，闭包节点签名与搬移前完全一致，行为零漂移。
T30-2（单层路由，``ROUTING_SINGLE_LAYER_ENABLED`` 默认关）：L1 已给出检索通道时
本节点消费转发进来的 ``route`` / ``kb_tools`` 并跳过模型调用（0 次决策）；
三段策略钳制**照旧执行**，两个来源共用同一段代码。flag-off 或 L1 未给参数
（兜底 / fast-path / 缓存命中）时，本函数体与搬移前逐句等价。
"""

from typing import Any, Dict

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate

from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_formatters import (
    history_to_text,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_states import (
    KBRouteDecision,
    KBWorkflowState,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.prompts.kb_prompts import (
    KB_ROUTER_HUMAN_TEMPLATE,
    KB_ROUTER_SYSTEM_TEMPLATE,
)
from agentix.infrastructure.core.logger import get_logger

kb_logger = get_logger(service="kb-router-node")


def create_router_node(llm: BaseChatModel, *, allow_external_search: bool):
    """构造 router 节点：LLM 路由到 local/external/hybrid，postgres 优先策略。"""
    router_prompt = ChatPromptTemplate.from_messages(
        [
            ("system", KB_ROUTER_SYSTEM_TEMPLATE),
            ("human", KB_ROUTER_HUMAN_TEMPLATE),
        ]
    )
    router_chain = router_prompt | llm.with_structured_output(KBRouteDecision)

    async def router(state: KBWorkflowState) -> Dict[str, Any]:
        # T30-2（单层路由）：L1 那次调用已经给出检索通道 ⇒ 本节点**不再花第二次
        # 模型调用**，直接消费转发进来的 ``route`` / ``kb_tools``。
        # 下面的三段策略钳制对**两个来源是同一份代码**——``allow_external_search``
        # 是安全边界而不是决策，漏掉它等于让 L1 越过 ``KB_ENABLE_EXTERNAL_SEARCH``。
        forwarded_route = state.get("route")
        if forwarded_route:
            route = forwarded_route
            tools_requested = state.get("kb_tools") or []
            rationale = "single-layer: consumed L1 routing output (no model call)"
        else:
            question = state.get("question", "")
            history_text = history_to_text(state.get("history", []))
            decision = await router_chain.ainvoke(
                {
                    "question": question,
                    "history": history_text,
                }
            )
            route = decision.route
            tools_requested = decision.tools or []
            rationale = decision.rationale

        if route in {"external", "hybrid"} and not allow_external_search:
            kb_logger.info(
                "Router requested {} but external search is disabled; using local instead.",
                route,
            )
            route = "local"
        tools = [tool for tool in tools_requested if tool in {"milvus", "postgres"}]
        if route != "external" and not tools:
            # 默认使用 postgres + milvus 兜底策略
            tools = ["postgres", "milvus"]
            kb_logger.info("Router 未指定工具，使用默认策略: postgres 优先 + milvus 兜底")
        kb_logger.info(
            "KB router decision: {} tools={} ({})",
            route,
            tools,
            rationale,
        )
        return {
            "route": route,
            "kb_tools": tools,
            "steps": ["router"],
        }

    return router
