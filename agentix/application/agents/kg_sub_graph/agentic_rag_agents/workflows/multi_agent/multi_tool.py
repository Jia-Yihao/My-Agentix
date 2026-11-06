from typing import Dict, List, Optional

from langchain_core.language_models import BaseChatModel
from langchain_neo4j import Neo4jGraph
from langgraph.constants import END, START
from langgraph.graph.state import CompiledStateGraph, StateGraph
from pydantic import BaseModel

# 导入输入输出状态定义
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.state import (
    InputState,
    OutputState,
    OverallState,
)
# 导入guardrails逻辑
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.guardrails.node import create_guardrails_node
# 导入分解节点
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.planner import create_planner_node
# 导入工具选择节点
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.tool_selection import create_tool_selection_node
# 导入 text2cypher 节点
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.cypher_tools import create_cypher_query_node
# 导入Cypher示例检索器基类
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.retrievers.cypher_examples.base import BaseCypherExampleRetriever
# 导入预定义Cypher节点
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.predefined_cypher import create_predefined_cypher_node
# 导入自定义工具函数节点
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.customer_tools import create_graphrag_query_node
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2cypher.text2sql_tool import create_text2sql_tool_node

from agentix.infrastructure.core.logger import get_logger


from ...components.errors import create_error_tool_selection_node
from ...components.final_answer import create_final_answer_node
# 批次 10（§10.10.1）：终态验证器适配节点（settings.KG_FINAL_ANSWER_VALIDATION
# 默认 False，关闭时 no-op 直通，零行为变化）。
from ...components.final_answer_guard import create_final_answer_guard



from ...components.summarize import create_summarization_node



from .edges import (
    guardrails_conditional_edge,
    map_reduce_planner_to_tool_selection,
)

from dataclasses import dataclass, field
# P1-1 拆分：KB 状态模型、格式化纯函数、5 个节点实现外置到同包子模块，
# 本文件只保留主工作流（create_multi_tool_workflow）与 KB 工作流的装配逻辑。
# 强制要求数据类中的所有字段必须以关键字参数的形式提供。即不能以位置参数的方式传递。
@dataclass(kw_only=True)
class AgentState(InputState):
    """The router's classification of the user's query."""
    steps: list[str] = field(default_factory=list)
    """Populated by the retriever. This is a list of documents that the agent can reference."""
    question: str = field(default_factory=str) # 这个参数用来与子图进行交互
    answer: str = field(default_factory=str)  # 这个参数用来与子图进行交互


def create_multi_tool_workflow(
    llm: BaseChatModel,
    graph: Neo4jGraph,
    tool_schemas: List[type[BaseModel]],
    predefined_cypher_dict: Dict[str, str],
    cypher_example_retriever: BaseCypherExampleRetriever,
    scope_description: Optional[str] = None,
    llm_cypher_validation: bool = True,
    max_attempts: int = 3,
    attempt_cypher_execution_on_final_attempt: bool = False,
    default_to_text2cypher: bool = True,
) -> CompiledStateGraph:
    """
    Create a multi tool Agent workflow using LangGraph.
    This workflow allows an agent to select from various tools to complete each identified task.

    Parameters
    ----------
    llm : BaseChatModel
        The LLM to use for processing
    graph : Neo4jGraph
        The Neo4j graph wrapper.
    tool_schemas : List[BaseModel]
        A list of Pydantic class defining the available tools.
    predefined_cypher_dict : Dict[str, str]
        A Python dictionary of Cypher query names as keys and Cypher queries as values.
    scope_description: Optional[str], optional
        A short description of the application scope, by default None
    cypher_example_retriever: BaseCypherExampleRetriever
        The retriever used to collect Cypher examples for few shot prompting.
    llm_cypher_validation : bool, optional
        Whether to perform LLM validation with the provided LLM, by default True
    max_attempts: int, optional
        The max number of allowed attempts to generate valid Cypher, by default 3
    attempt_cypher_execution_on_final_attempt, bool, optional
        THIS MAY BE DANGEROUS.
        Whether to attempt Cypher execution on the last attempt, regardless of if the Cypher contains errors, by default False
    default_to_text2cypher : bool, optional
        Whether to attempt Text2Cypher if no tool calls are returned by the LLM, by default True
    initial_state: Optional[InputState], optional
        An initial state passed from parent graph, by default None

    Returns
    -------
    CompiledStateGraph
        The workflow.
    """
    # 1. 创建guardrails节点
    # Guardrails 节点决定传入的问题是否在检索的范围内（比如是否和电商（自家的产品相关））。如果不在，则提供默认消息，并且工作流路由到最终的答案生成。
    guardrails = create_guardrails_node(
        llm=llm, graph=graph, scope_description=scope_description
    )

    # 2. 如果通过guardrails，则会针对用户的问题进行任务分解
    planner = create_planner_node(llm=llm)

    # 3. 创建cypher_query节点，用来根据用户的问题生成Cypher查询语句 大模型生成Cypher查询语句
    cypher_query = create_cypher_query_node()

    predefined_cypher = create_predefined_cypher_node(
        graph=graph, predefined_cypher_dict=predefined_cypher_dict
    ) #预定义的自定Cypher查询语句

    customer_tools = create_graphrag_query_node() # lightrag_query
    text2sql_query = create_text2sql_tool_node(graph)

    # 工具选择节点，根据用户的问题选择合适的工具
    tool_selection = create_tool_selection_node(
        llm=llm,
        tool_schemas=tool_schemas,
        default_to_text2cypher=default_to_text2cypher,
    )
    summarize = create_summarization_node(llm=llm)

    final_answer = create_final_answer_node()

    # 创建状态图运行时会维护一个“全局状态”（OverallState），入口状态类型是 InputState，最终产出是 OutputState。节点函数读写的就是这个状态。
    main_graph_builder = StateGraph(
        OverallState,
        input_schema=InputState,
        output_schema=OutputState,
    )

    main_graph_builder.add_node(guardrails)# 安全护栏敏感内容过滤、权限/配额校验
    main_graph_builder.add_node(planner) #决定下一步要用的工具/路径。

    # 节点名必须**显式给出**：如 `customer_tools` 对应的函数实际叫 `lightrag_query`，
    # 若依赖 LangGraph 从 `__name__` 推导会改名，导致已注册的边找不到节点
    # （批次 13 实测踩到：`add_node(customer_tools)` 会注册成 `lightrag_query`，
    #  于是边 `customer_tools` 报 "unknown node"）。
    #
    # 注：曾尝试在此插入「执行前人工审批」的 LangGraph `interrupt()` 中断点，实测结论：
    #   - **async 节点不可用**：`interrupt()` 抛
    #     "Called get_configurable outside of a runnable context"；
    #   - **sync 节点可用**（最小复现通过：暂停 → `Command(resume=…)` 恢复并取回裁决值）。
    # 本仓库工具节点**均为 async**，故包装式接线不成立；可行做法是另插一个 **sync 审批节点**
    # 并改条件边（属图拓扑变更，影响主路由）。
    # 权衡后**撤回** —— 宁可不做，也不留一个点了开关就炸的中断点。
    # 另注：`langgraph-checkpoint==2.0.26` 并非"大版本不匹配"——langgraph 0.2.60 自身声明
    # `langgraph-checkpoint>=2.0.4,<3.0.0`，该 pin 是正确的。
    main_graph_builder.add_node("cypher_query", cypher_query)
    main_graph_builder.add_node("predefined_cypher", predefined_cypher)
    main_graph_builder.add_node("customer_tools", customer_tools)  # lightrag_query
    main_graph_builder.add_node("text2sql_query", text2sql_query)
    main_graph_builder.add_node(summarize) # 总结
    main_graph_builder.add_node(tool_selection) #工具选择的中间控制节点（通常结合 planner 的输出）。
    main_graph_builder.add_node(final_answer)
    # 批次 10（§10.10.1）：终态验证 guard（summarize → final_answer_guard → final_answer）。
    # 默认关（settings.KG_FINAL_ANSWER_VALIDATION=False）：guard 直通 no-op。
    main_graph_builder.add_node("final_answer_guard", create_final_answer_guard(llm=llm, graph=graph))
    # 工具选择失败兜底节点：tool_selection 在 default_to_text2cypher=False 时会 goto 到这里，
    # 必须注册，否则 LangGraph 运行时抛 "node not found"。
    main_graph_builder.add_node("error_tool_selection", create_error_tool_selection_node())


    # 添加边
    main_graph_builder.add_edge(START, "guardrails")
    main_graph_builder.add_conditional_edges(
        "guardrails",
        guardrails_conditional_edge,
    ) #这是条件边：执行完 guardrails 后，不是固定跳到某个节点，而是调用 guardrails_conditional_edge(state) 来返回下一跳的节点名（或一个映射）。
    main_graph_builder.add_conditional_edges(
        "planner",
        map_reduce_planner_to_tool_selection, #据 planner 写进 state 的结果，返回下一个要去的节点名
        ["tool_selection"], #从 planner 出来只能跳到 "tool_selection"，且由 map_reduce_planner_to_tool_selection(state) 来决定（但这里其实被限制成只能选这一个）。
    )

    main_graph_builder.add_edge("cypher_query", "summarize")
    main_graph_builder.add_edge("predefined_cypher", "summarize")
    main_graph_builder.add_edge("customer_tools", "summarize")
    main_graph_builder.add_edge("text2sql_query", "summarize")
    # error_tool_selection 节点返回普通 state 增量（非 Command），需要显式出边收敛到 summarize，避免挂起。
    main_graph_builder.add_edge("error_tool_selection", "summarize")
    # 批次 10（§10.10.1）：summarize 后先过终态验证 guard，再到 final_answer。
    main_graph_builder.add_edge("summarize", "final_answer_guard")
    main_graph_builder.add_edge("final_answer_guard", "final_answer")

    main_graph_builder.add_edge("final_answer", END)

    return main_graph_builder.compile()


kb_logger = get_logger(service="kb-multi-tool")

# P1-1 拆分：KB 状态模型与格式化纯函数外置（本文件只保留装配逻辑）


# P1-1 拆分：KB 多工具工作流装配已外置到 kb_workflow.py（保持原导入路径兼容）
from .kb_workflow import create_kb_multi_tool_workflow  # noqa: F401
