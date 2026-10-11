"""
This code is based on content found in the LangGraph documentation: https://python.langchain.com/docs/tutorials/graph/#advanced-implementation-with-langgraph
"""

from typing import Optional
from langchain_core.prompts import ChatPromptTemplate
from langchain_neo4j import Neo4jGraph
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.utils.utils import retrieve_and_parse_schema_from_graph_for_prompts
from agentix.application.agents.kg_sub_graph.prompts.kg_prompts import GUARDRAILS_SYSTEM_PROMPT
from agentix.application.agents.lg_prompts import (
    GUARDRAILS_GRAPH_PREFIX,
    GUARDRAILS_QUESTION_SUFFIX,
    GUARDRAILS_SCOPE_PREFIX,
)


def create_guardrails_prompt_template(
    graph: Optional[Neo4jGraph] = None, scope_description: Optional[str] = None
) -> ChatPromptTemplate:
    """
    Create a guardrails prompt template.

    Parameters
    ----------
    graph : Optional[Neo4jGraph], optional
        The `Neo4jGraph` object used to generated a schema definition, by default None
    scope_description : Optional[str], optional
        A description of the application scope, by default None

    Returns
    -------
    ChatPromptTemplate
        The prompt template.
    """
    # 前缀字面量复用 lg_prompts 的共享常量（单一事实源，P2-3 残留收口）。
    # ⚠️ 用 f-string 内插而非 `常量 + expr`：f-string 经 __format__ 把任何对象
    # 强制成 str（与原内联 f-string 行为逐字节一致）；纯 `+` 遇到非 str（如
    # 测试夹具的 MagicMock 图）会触发 __radd__ 返回 mock，进而炸掉模板构造。
    scope_context = (
        f"{GUARDRAILS_SCOPE_PREFIX}{scope_description}"
        if scope_description is not None
        else ""
    )

    # 动态从 Neo4j 图表中获取图表结构
    graph_context = (
        f"{GUARDRAILS_GRAPH_PREFIX}{retrieve_and_parse_schema_from_graph_for_prompts(graph)}"
        if graph is not None
        else ""
    )

    message = scope_context + graph_context + GUARDRAILS_QUESTION_SUFFIX

    return ChatPromptTemplate.from_messages(
        [
            (
                "system",
                GUARDRAILS_SYSTEM_PROMPT,
            ),
            (
                "human",
                (message),
            ),
        ]
    )
