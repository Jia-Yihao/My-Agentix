"""
Guardrails module - scope checking and additional info gathering.
"""

from typing import Dict, List, Literal

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableConfig
from loguru import logger
from pydantic import BaseModel, Field

from agentix.application.agents.constants import SCOPE_DESCRIPTION, GUARDRAILS_REJECT_MESSAGE
from agentix.orchestration.langgraph.lg_states import AgentState
from agentix.application.agents.lg_prompts import (
    GET_ADDITIONAL_LOGIC_PROMPT,
    GET_ADDITIONAL_SYSTEM_PROMPT,
    GUARDRAILS_GRAPH_PREFIX,
    GUARDRAILS_QUESTION_SUFFIX,
    GUARDRAILS_SCOPE_PREFIX,
    GUARDRAILS_SYSTEM_PROMPT,
)
from agentix.application.agents.llm_factory import get_llm_client
from agentix.application.agents.router import _ensure_router
from agentix.application.agents.kg_sub_graph.kg_neo4j_conn import get_neo4j_graph
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.utils.utils import (
    retrieve_and_parse_schema_from_graph_for_prompts,
)


class AdditionalGuardrailsOutput(BaseModel):
    """Structured output for guardrails decision."""
    decision: Literal["end", "proceed"] = Field(
        description="Decision on whether the question is related to the graph contents."
    )


async def get_additional_info(state: AgentState, *, config: RunnableConfig) -> Dict[str, List[BaseMessage]]:
    """Generate a response requesting more information from the user.

    When the router determines more information is needed, this function is called.
    It checks guardrails first to ensure the query is within scope.
    """
    logger.info("------continue to get additional info------")

    # P0-2(b) 未接线（后续项）：同一 llm 实例既跑 guardrails 内联判定链，
    # 又跑面向用户的回答调用（下方 model.ainvoke），二者温度/超时诉求不同，
    # 需 per-call 注入才能差异化，已列为后续项。
    model = get_llm_client(temperature=0.7, tags=["additional_info"])

    # Connect to Neo4j graph database
    try:
        neo4j_graph = get_neo4j_graph()
        logger.info("success to get Neo4j graph database connection")
    except Exception as e:
        logger.error("failed to get Neo4j graph database connection: %s", e)
        neo4j_graph = None

    scope_context = f"{GUARDRAILS_SCOPE_PREFIX}{SCOPE_DESCRIPTION}"
    graph_context = (
        f"{GUARDRAILS_GRAPH_PREFIX}{retrieve_and_parse_schema_from_graph_for_prompts(neo4j_graph)}"
        if neo4j_graph is not None
        else ""
    )

    message = scope_context + graph_context + GUARDRAILS_QUESTION_SUFFIX

    full_system_prompt = ChatPromptTemplate.from_messages([
        ("system", GUARDRAILS_SYSTEM_PROMPT),
        ("human", message),
    ])

    guardrails_chain = full_system_prompt | model.with_structured_output(AdditionalGuardrailsOutput)
    # P1-4（重要）：此处**故意不转发 config**。本节点（get_additional_info）在流式
    # 白名单内，若把 config 传给这条「内部判定链」，其结构化输出 token 会以
    # langgraph_node="get_additional_info" 冒泡出去，被前端当成答案流——属于内部
    # 编排外泄。白名单按「节点」过滤，无法区分同节点内的不同调用点，故必须在源头
    # 控制：只有真正面向用户的回答调用（下方 model.ainvoke）才转发 config。
    guardrails_output = await guardrails_chain.ainvoke(
        {"question": state.messages[-1].content if state.messages else ""}
    )

    if guardrails_output is None:
        logger.warning("Guardrails returned None, defaulting to proceed")
        guardrails_output = AdditionalGuardrailsOutput(decision="proceed")

    if guardrails_output.decision == "end":
        logger.info("-----Fail to pass guardrails check-----")
        return {"messages": [AIMessage(content=GUARDRAILS_REJECT_MESSAGE)]}
    else:
        logger.info("-----Pass guardrails check-----")
        router = _ensure_router(
            getattr(state, "router", None),
            fallback_question=state.messages[-1].content if state.messages else "",
        )
        state.router = router
        # P2-3（静态前缀）：静态系统指令置首（跨请求稳定，利于前缀 KV 缓存），动态
        # 分类理由作为其后的独立消息，不再插值进系统消息内容。
        messages = [
            {"role": "system", "content": GET_ADDITIONAL_SYSTEM_PROMPT},
            {"role": "system", "content": GET_ADDITIONAL_LOGIC_PROMPT.format(logic=router.logic)},
        ] + state.messages
        # P1-4：面向用户的回答调用，转发 config → 逐 token 真流式。
        response = await model.ainvoke(messages, config=config)
        return {"messages": [response]}
