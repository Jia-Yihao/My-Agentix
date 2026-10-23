"""KB guardrails 节点（P1-1 拆分，从 multi_tool.py 搬移）。

工厂注入依赖，闭包节点签名与搬移前完全一致，行为零漂移。
"""

from typing import Any, Dict

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate

from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_states import (
    KBGuardrailsDecision,
    KBWorkflowState,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.prompts.kb_prompts import (
    KB_GUARDRAILS_HUMAN_TEMPLATE,
    KB_GUARDRAILS_SYSTEM_TEMPLATE,
)
from agentix.infrastructure.core.logger import get_logger

kb_logger = get_logger(service="kb-guardrails-node")


def create_guardrails_node(llm: BaseChatModel, *, scope_text: str):
    """构造 guardrails 节点：安全审查，越界问题置 decision='end'。"""
    guardrails_prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                KB_GUARDRAILS_SYSTEM_TEMPLATE.format(scope_text=scope_text),
            ),
            ("human", KB_GUARDRAILS_HUMAN_TEMPLATE),
        ]
    )
    guardrails_chain = guardrails_prompt | llm.with_structured_output(KBGuardrailsDecision)

    async def guardrails(state: KBWorkflowState) -> Dict[str, Any]:
        question = state.get("question", "")
        decision = await guardrails_chain.ainvoke({"question": question})
        summary = decision.summary or (
            "抱歉，该问题不在测试数据集知识库的支持范围内，请询问数据条目释义、数据来源背景或相关测试数据规定等内容。"
            if decision.decision == "end"
            else ""
        )
        kb_logger.info("KB guardrails decision: {}", decision.decision)
        return {
            "guardrails_decision": decision.decision,
            "summary": summary,
            "steps": ["guardrails"],
        }

    return guardrails
