"""
This code is based on content found in the LangGraph documentation: https://python.langchain.com/docs/tutorials/graph/#advanced-implementation-with-langgraph

DEAD-CODE(P2-5)｜处置=保留·平行变体家族：本文件属 `ps_genai_agents` 平行变体家族，
**全仓无生产引用**——当前主链路的 guardrails prompt 由
`kg_sub_graph/prompts/schema_utils.create_guardrails_context` 构建。
按方案 P2-5 约定：**不接线、不删除**，仅保持可导入、可审查。

2026-09 授权清理时复核：用户已明确授权删除死代码，但本家族**仍然保留**——
理由是它不是"无人认领的残骸"：①`test_p36_structure_hygiene` 有专门的
导入健康守卫（`test_ps_genai_agents_family_imports_cleanly`）；
②`test_p33_task_planning` 直接断言本文件的 prompt 构建行为；
③家族共 4 文件 / 69 行，维护成本极低。删除需一并改动上述两处测试，
收益不抵风险，故处置为**保留**而非删除。

批次 13 修复：删除了一条 `from ..utils.utils import ...` 的 import——
该**目标模块根本不存在**（`components/` 下无 `utils` 包），且被导入的符号在本文件
中从未使用（函数体用的是 `create_guardrails_context`）。它使本文件**永久无法导入**，
任何试图 import 或静态分析它的工具都会报 ModuleNotFoundError。
移除后本文件可正常导入，行为零变化。
"""

from typing import Optional

from langchain_core.prompts import ChatPromptTemplate
from langchain_neo4j import Neo4jGraph

from agentix.application.agents.kg_sub_graph.prompts.kg_prompts import GUARDRAILS_SYSTEM_PROMPT
from agentix.application.agents.kg_sub_graph.prompts import create_guardrails_context

guardrails_system = """
You must decide whether the provided question is in scope.
Assume the question might be related.
If you're absolutely sure it is NOT related, output "end".
Provide only the specified output: "planner" or "end".
"""




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
    # 使用新的安全schema工具获取上下文
    context = create_guardrails_context(graph=graph, scope_description=scope_description)

    # 构建消息模板
    message = context["scope_context"] + context["graph_schema"] + "Question: {question}"

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
