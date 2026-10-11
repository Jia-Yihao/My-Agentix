"""KB router 节点（P1-1 拆分，从 multi_tool.py 搬移）。

工厂注入依赖，闭包节点签名与搬移前完全一致，行为零漂移。
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
        question = state.get("question", "")
        history_text = history_to_text(state.get("history", []))
        decision = await router_chain.ainvoke(
            {
                "question": question,
                "history": history_text,
            }
        )
        route = decision.route
        if route in {"external", "hybrid"} and not allow_external_search:
            kb_logger.info(
                "Router requested {} but external search is disabled; using local instead.",
                route,
            )
            route = "local"
        tools = [tool for tool in decision.tools or [] if tool in {"milvus", "postgres"}]
        if route != "external" and not tools:
            # 默认使用 postgres + milvus 兜底策略
            tools = ["postgres", "milvus"]
            kb_logger.info("Router 未指定工具，使用默认策略: postgres 优先 + milvus 兜底")
        kb_logger.info(
            "KB router decision: {} tools={} ({})",
            route,
            tools,
            decision.rationale,
        )
        return {
            "route": route,
            "kb_tools": tools,
            "steps": ["router"],
        }

    return router
