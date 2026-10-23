"""
Tool selection node for orchestrating legal knowledge graph tools.

This node inspects an incoming question, asks the LLM to pick an appropriate tool
(template-based Cypher, text-to-Cypher, or customer-specific tools), and routes
execution accordingly.
"""

from typing import Any, Callable, Coroutine, Dict, List, Literal, Set, Tuple
import re

from langchain_core.language_models import BaseChatModel
from langchain_core.output_parsers import PydanticToolsParser
from langchain_core.runnables.base import Runnable
from langgraph.types import Command, Send
from pydantic import BaseModel

from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.state import (
    ToolSelectionInputState,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.tool_selection.prompts import (
    create_tool_selection_prompt_template,
)
from agentix.domain.errors import AgentError, ErrorCode
from agentix.infrastructure.core.logger import get_logger

# Pre-build the prompt template so we can reuse it across calls.
tool_selection_prompt = create_tool_selection_prompt_template()
logger = get_logger(service="tool-selection")

SQL_KEYWORDS = [
    "sql",
    "统计",
    "报表",
    "同比",
    "环比",
    "平均",
    "总数",
    "数量",
    "占比",
    "select",
    "count",
    "sum",
    "avg",
    "max",
    "min",
    "group by",
    "order by",
    "having",
    "where",
    "insert",
    "update",
    "delete",
]
SQL_PATTERN = re.compile(r"\b(select|count|sum|avg|max|min|group\s+by|order\s+by)\b", re.IGNORECASE)

DESCRIPTIVE_KEYWORDS = [
    "释义",
    "概念",
    "定义",
    "背景",
    "历史",
    "适用",
    "范围",
    "要点",
    "解读",
    "概述",
    "简介",
    "沿革",
]


def _looks_like_sql_question(question: str) -> bool:
    normalized = question.lower()
    if SQL_PATTERN.search(normalized):
        return True
    return any(keyword in normalized for keyword in SQL_KEYWORDS)


def _make_command(target: str, payload: Dict[str, Any]) -> Command[Any]:
    return Command(goto=Send(target, payload))


# P1-3c: 查表分发——tool_name -> (goto 节点名, payload 模式)。
# payload 模式：
#   "args"     -> Send payload.query_parameters 直接用 LLM 的 tool_args
#   "question" -> cypher_query 历史契约：query_parameters 固定 {"question": 原问题}
_TOOL_ROUTE_TABLE: Dict[str, Tuple[str, str]] = {
    "predefined_cypher": ("predefined_cypher", "args"),
    "cypher_query": ("cypher_query", "question"),
    "text2sql_query": ("text2sql_query", "args"),
}


def _resolve_route(tool_name: str, available_tools: Set[str]) -> Tuple[str, str]:
    """查表解析工具路由；未注册工具名抛 AgentError(TOOL_NOT_FOUND)。

    - 直连表：predefined_cypher / cypher_query / text2sql_query 各有专属节点
    - 兜底表：动态注册工具统一进 customer_tools（如 microsoft_graphrag_query）
    - 双未命中 -> 显式错误（方案 P1-3 验收 3：不静默降级）
    """
    route = _TOOL_ROUTE_TABLE.get(tool_name)
    if route is not None:
        return route
    if tool_name in available_tools:
        return ("customer_tools", "args")
    raise AgentError(
        ErrorCode.TOOL_NOT_FOUND,
        f"未注册的工具: {tool_name}",
        detail=f"available tools: {sorted(available_tools)}",
    )


def create_tool_selection_node(
    llm: BaseChatModel,
    tool_schemas: List[type[BaseModel]],
    default_to_text2cypher: bool = True,
) -> Callable[[ToolSelectionInputState], Coroutine[Any, Any, Command[Any]]]:
    """
    Create the LangGraph node that decides which tool to invoke for a given task.
    """

    tool_selection_chain: Runnable[Dict[str, Any], Any] = (
        tool_selection_prompt
        | llm.bind_tools(tools=tool_schemas)
        | PydanticToolsParser(tools=tool_schemas, first_tool_only=True)
    )

    # R3-4（架构文档 §17）：带策略时的过滤链缓存——避免同一 policy 重复重绑
    _policy_chain_cache: Dict[Any, Runnable[Dict[str, Any], Any]] = {}

    def _chain_for_policy(policy: Any) -> Runnable[Dict[str, Any], Any]:
        """按请求策略返回选择链：无策略/全量可见时复用预建链（零行为变化）。"""
        if policy is None:
            return tool_selection_chain
        from agentix.tools.selection import select_tool_schemas

        cached = _policy_chain_cache.get(id(policy))
        if cached is not None:
            return cached
        filtered = select_tool_schemas(tool_schemas, policy)
        if len(filtered) == len(tool_schemas):
            chain = tool_selection_chain
        else:
            chain = (
                tool_selection_prompt
                | llm.bind_tools(tools=filtered)
                | PydanticToolsParser(tools=filtered, first_tool_only=True)
            )
        _policy_chain_cache[id(policy)] = chain
        return chain

    # Record tool titles for quick membership checks.
    available_tools: Set[str] = {
        schema.model_json_schema().get("title", "") for schema in tool_schemas
    }

    async def tool_selection(
        state: ToolSelectionInputState,
        config: Dict[str, Any] | None = None,
    ) -> Command[Literal["cypher_query", "predefined_cypher", "customer_tools", "text2sql_query"]]:
        """
        Choose the appropriate tool for the given task.
        """

        question_text = state.get("question", "")
        context = state.get("context") or {}
        route_type = (context.get("route_type") or "").lower()

        logger.info(
            "Tool selection invoked",
            extra={
                "question": question_text,
                "route_type": route_type or "unknown",
            },
        )

        # Heuristic fast path for结构化查询
        if any(keyword in question_text for keyword in DESCRIPTIVE_KEYWORDS):
            logger.info("检测到测试数据描述类需求，优先使用 GraphRAG。")
            return _make_command(
                "customer_tools",
                {
                    "task": question_text,
                    "query_name": "microsoft_graphrag_query",
                    "query_parameters": {"query": question_text},
                    "steps": ["tool_selection"],
                },
            )

        if _looks_like_sql_question(question_text) and route_type == "text2sql-query":
            logger.info("Detect text2sql intent via route_type/keywords，直接调用 Text2SQL 工具。")
            return _make_command(
                "text2sql_query",
                {
                    "task": question_text,
                    "query_name": "text2sql_query",
                    "query_parameters": {},
                    "steps": ["tool_selection"],
                },
            )

        # Heuristic for GraphRAG/LightRAG - 改为让 LLM 动态选择工具，而不是硬编码
        # if route_type == "graphrag-query":
        #     logger.info("Router 标记为 graphrag-query，优先使用 GraphRAG 工具。")
        #     return _make_command(
        #         "customer_tools",
        #         {
        #             "task": question_text,
        #             "query_name": "microsoft_graphrag_query",
        #             "query_parameters": {"query": question_text},
        #             "steps": ["tool_selection"],
        #         },
        #     )

        go_to_text2cypher: Command[
            Literal["cypher_query", "predefined_cypher", "customer_tools", "text2sql_query"]
        ] = Command(
            goto=Send(
                "cypher_query",
                {
                    "task": state.get("question", ""),
                    "query_name": "cypher_query",
                    "query_parameters": {"question": state.get("question", "")},
                    "steps": ["tool_selection"],
                },
            )
        )

        # R3-4：configurable["tool_policy"] 存在时按策略过滤 LLM 可见工具
        # （缺省 None = 不过滤，行为与既有完全一致）
        _policy = ((config or {}).get("configurable") or {}).get("tool_policy")
        chain = _chain_for_policy(_policy)

        tool_selection_output: BaseModel | None = await chain.ainvoke(
            {"question": state.get("question", "")}
        )

        if tool_selection_output is not None:
            tool_name: str = tool_selection_output.model_json_schema().get("title", "")
            tool_args: Dict[str, Any] = tool_selection_output.model_dump()
            logger.info(
                "LLM 选择工具",
                extra={
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "route_type": route_type or "unknown",
                },
            )

            if tool_name:
                # P1-3c: 查表分发替代 if 链——新增工具无需改此节点
                goto, mode = _resolve_route(tool_name, available_tools)
                if mode == "question":
                    # cypher_query 历史契约：query_parameters 固定为原问题
                    return go_to_text2cypher
                return _make_command(
                    goto,
                    {
                        "task": question_text,
                        "query_name": tool_name,
                        "query_parameters": tool_args,
                        "steps": ["tool_selection"],
                    },
                )

        if default_to_text2cypher:
            logger.info("LLM 未给出明确工具，回退到 cypher_query。")
            return go_to_text2cypher

        logger.warning("无法确定工具，进入 error_tool_selection。")
        return _make_command(
            "error_tool_selection",
            {
                "task": question_text,
                "errors": [
                    f"Unable to assign tool to question: `{question_text}`"
                ],
                "steps": ["tool_selection"],
            },
        )

    return tool_selection
