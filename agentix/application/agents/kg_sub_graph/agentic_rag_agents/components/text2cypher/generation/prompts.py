"""
This code is based on content found in the LangGraph documentation: https://python.langchain.com/docs/tutorials/graph/#advanced-implementation-with-langgraph
"""

from langchain_core.prompts import ChatPromptTemplate

from agentix.config import settings

from agentix.application.agents.kg_sub_graph.agentic_rag_agents.retrievers.cypher_examples.legal_retriever import (
    LEGAL_GRAPH_SCHEMA,
)


def legacy_schema_hint() -> str:
    """静态中文标签冗余块的调用期取值（Stage F2b-d·R-P2-3）。

    - flag-off（``KG_SCHEMA_AWARE_GENERATION_ENABLED`` 默认关）＝
      ``LEGAL_GRAPH_SCHEMA`` 全文（与基线拼接结果逐字节等价）；
    - flag-on ＝空串——动态 ``{schema}``（发现态渲染）成为唯一权威源，
      静态硬编码标签描述退出提示词。
    """
    if bool(getattr(settings, "KG_SCHEMA_AWARE_GENERATION_ENABLED", False)):
        return ""
    return LEGAL_GRAPH_SCHEMA


def create_text2cypher_generation_prompt_template() -> ChatPromptTemplate:
    """
    Create a Text2Cypher generation prompt template.

    Returns
    -------
    ChatPromptTemplate
        The prompt template.
    """
    return ChatPromptTemplate.from_messages(
        [
            (
                "system",
                (
                    "根据输入的问题，将其转换为Cypher查询语句。不要添加任何前言。"
                    "不要在响应中包含任何反引号或其他标记。注意：只返回Cypher语句！"
                ),
            ),
            (
                "human",
                (
                    """你是一位Neo4j专家。根据输入的问题，创建一个语法正确的Cypher查询语句。
                        不要在响应中包含任何反引号或其他标记。只使用MATCH或WITH子句开始查询。只返回Cypher语句！

                        以下是数据库模式信息：
                        {schema}

                        下面是一些问题和对应Cypher查询的示例：

                        {fewshot_examples}

                        以下是测试数据集图谱的静态 Schema 说明（冗余保险，动态 schema 以 {schema} 为准）：
{legacy_schema_hint}

                        用户输入: {question}
                        Cypher查询:"""
                ),
            ),
        ]
    )
