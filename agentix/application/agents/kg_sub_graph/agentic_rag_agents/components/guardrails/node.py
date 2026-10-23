"""
This code is based on content found in the LangGraph documentation: https://python.langchain.com/docs/tutorials/graph/#advanced-implementation-with-langgraph
"""

from typing import Any, Callable, Coroutine, Dict, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables.base import Runnable
from langchain_neo4j import Neo4jGraph


from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.guardrails.models import GuardrailsOutput
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.guardrails.prompts import create_guardrails_prompt_template
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.state import InputState
from agentix.infrastructure.core.logger import get_logger

# 获取日志记录器
logger = get_logger(service="guardrails_node")


def create_guardrails_node(
    llm: BaseChatModel,
    graph: Optional[Neo4jGraph] = None,
    scope_description: Optional[str] = None,
) -> Callable[[InputState], Coroutine[Any, Any, dict[str, Any]]]:
    """
    Create a guardrails node to be used in a LangGraph workflow.

    Parameters
    ----------
    llm : BaseChatModel
        The LLM used to process data.
    graph: Optional[Neo4jGraph], optional
        The `Neo4jGraph` object used to generated a schema definition, by default None
    scope_description : Optional[str], optional
        A description of the application scope, by default None

    Returns
    -------
    Callable[[InputState], OverallState]
        The LangGraph node.
    """

    # 获取包含了图表结构和范围描述的guardrails完整提示词
    guardrails_prompt = create_guardrails_prompt_template(
        graph=graph, scope_description=scope_description
    )

    # 使用LLM进行结构化输出
    guardrails_chain: Runnable[Dict[str, Any], Any] = (
        guardrails_prompt | llm.with_structured_output(GuardrailsOutput)
    )

    async def guardrails(state: InputState) -> Dict[str, Any]:
        """
        Decides if the question is in scope.
        """

        # 提取到输入的问题
        question = state.get("question", "")

        heuristics_keywords = [
            "法条",
            "法规",
            "法律",
            "民法典",
            "刑法",
            "行政法",
            "案由",
            "司法解释",
            "条文",
            "民事",
            "刑事",
            "行政",
            "合同",
            "侵权",
            "赔偿",
            "婚姻",
            "劳动",
            "诉讼",
            "判决",
            "效力层级",
            "制定机关",
            "上位法",
        ]

        if any(keyword in question for keyword in heuristics_keywords) or "?" in question or "？" in question:
            logger.info(
                "Guardrails 前置规则命中测试数据集领域关键词，直接进入 planner。",
                extra={"question": question},
            )
            return {"next_action": "planner", "summary": None, "steps": ["guardrails"]}

        # 使用LLM进行结构化输出
        try:
            guardrails_output: GuardrailsOutput = await guardrails_chain.ainvoke(
                {"question": question}
            )
        except Exception as exc:  # pragma: no cover - 容错
            logger.warning("Guardrails LLM 调用失败，回退到 planner: %s", exc)
            return {"next_action": "planner", "summary": None, "steps": ["guardrails"]}

        decision = guardrails_output.decision
        summary = None
        if decision == "end":
            if any(keyword in question for keyword in heuristics_keywords) or "?" in question or "？" in question:
                logger.info(
                    "Guardrails 触发兜底：问题含有测试数据集领域关键词，强制路由到 planner。",
                    extra={"question": question},
                )
                decision = "planner"

        if decision == "end":
            summary = "抱歉，这个问题不在测试数据集问答范围内，您可以问我关于数据条目、数据分类、示例说明等方面的内容哦~"

        decision_info = {
            "next_action": decision,
            "summary": summary,
            "steps": ["guardrails"],
        }

        logger.info(f"Guardrails Decision Info: {decision_info}")

        return decision_info


    return guardrails
