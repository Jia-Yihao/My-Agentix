"""
This code is based on content found in the LangGraph documentation: https://python.langchain.com/docs/tutorials/graph/#advanced-implementation-with-langgraph
"""

from typing import Any, Callable, Coroutine, Dict, List

from langchain_neo4j import Neo4jGraph

from agentix.tools.gateway.kg_gateway import run_governed
from agentix.application.agents.kg_sub_graph.kg_neo4j_conn import query_readonly
from agentix.infrastructure.core.logger import get_logger

from ....constants import NO_CYPHER_RESULTS, WRITE_CLAUSES
from ..state import CypherOutputState, CypherState

logger = get_logger(service="text2cypher-execution")


def create_text2cypher_execution_node(
    graph: Neo4jGraph,
) -> Callable[
    [CypherState], Coroutine[Any, Any, Dict[str, List[CypherOutputState] | List[str]]]
]:
    """
    Create a Text2Cypher execution node for a LangGraph workflow.

    Parameters
    ----------
    graph : Neo4jGraph
        The Neo4j graph wrapper.

    Returns
    -------
    Callable[[CypherState], Dict[str, List[CypherOutputState] | List[str]]]
        The LangGraph node.
    """

    async def execute_cypher(
        state: CypherState,
    ) -> Dict[str, List[CypherOutputState] | List[str]]:
        """
        Executes the given Cypher statement.

        执行侧二次只读闸门（P0）：validation 节点的写检查可能因
        `attempt_cypher_execution_on_final_attempt=True` 被跳过，
        因此执行前必须再做一次黑名单校验，绝不执行含写子句的语句。
        """
        statement = state.get("statement", "")

        # 执行侧只读闸门：命中写子句 → 拒绝执行，不触碰数据库
        upper_statement = statement.upper()
        write_errors = [
            f"Cypher contains write clause: {wc}"
            for wc in WRITE_CLAUSES
            if wc in upper_statement
        ]

        records = None
        if write_errors:
            # 闸门拦截：不执行，仅记录错误
            logger.warning("execute_cypher blocked write query: {}", write_errors)
        else:
            records = await run_governed(
                "cypher_query",
                {"statement": statement},
                # 上游软闸已放行 → query_readonly 的只读闸为二道防线（语句
                # 必为干净语句）；NEO4J_QUERY_TIMEOUT_SECONDS>0 时限时执行（R-P0-4）
                direct=lambda: query_readonly(graph, statement),
            )

        steps = state.get("steps", list())
        steps.append("execute_cypher")

        errors = list(state.get("errors", list()))
        errors.extend(write_errors)

        return {
            "cyphers": [
                CypherOutputState(
                    **{
                        "task": state.get("task", []),
                        "statement": statement,
                        "parameters": None,
                        "errors": errors,
                        "records": records if records else NO_CYPHER_RESULTS,
                        "steps": steps,
                    }
                )
            ],
            "steps": ["text2cypher"],
        }

    return execute_cypher
