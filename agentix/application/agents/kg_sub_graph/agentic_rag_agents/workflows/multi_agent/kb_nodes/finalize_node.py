"""KB finalize 节点（P1-1 拆分，从 multi_tool.py 搬移）。"""


from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableConfig

from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_formatters import (
    collect_sources,
    format_combined_local_results,
    format_external_results,
    format_milvus_results,
    format_postgres_results,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_states import (
    KBOutputState,
    KBWorkflowState,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.prompts.kb_prompts import (
    KB_FINAL_HUMAN_TEMPLATE,
    KB_FINAL_SYSTEM_TEMPLATE,
)
from agentix.infrastructure.core.logger import get_logger

kb_logger = get_logger(service="kb-finalize-node")


def create_finalize_node(llm: BaseChatModel, *, top_k: int):
    """构造 finalize 节点：整合检索结果并合成最终回答。"""
    final_prompt = ChatPromptTemplate.from_messages(
        [
            ("system", KB_FINAL_SYSTEM_TEMPLATE),
            ("human", KB_FINAL_HUMAN_TEMPLATE),
        ]
    )

    # P1-4：声明 config（langgraph 按签名注入）并转发给 LLM 调用，否则
    # 本节点的 chat model 事件不会冒泡到主图的 astream_events，真流式失效。
    # 注意：本节点名 ``finalize`` 已在 chat_service 的流式白名单内。
    async def finalize(state: KBWorkflowState, *, config: RunnableConfig) -> KBOutputState:
        degraded = bool(state.get("degraded", False))
        if state.get("guardrails_decision") == "end":
            summary = state.get("summary") or "抱歉，该问题暂时无法回答。"
            return {
                "answer": summary,
                "sources": [],
                "steps": ["finalize"],
                "degraded": False,
            }

        milvus_results = state.get("milvus_results", [])
        postgres_results = state.get("postgres_results", [])
        local_results = state.get("local_results", []) or (milvus_results + postgres_results)
        external_results = state.get("external_results", [])

        milvus_context = format_milvus_results(milvus_results, top_k=top_k)
        postgres_context = format_postgres_results(postgres_results, top_k=top_k)
        local_context = format_combined_local_results(local_results, top_k=top_k)
        external_context = format_external_results(external_results, top_k=top_k)

        sources = collect_sources(milvus_results, postgres_results, external_results)

        if not local_results and not external_results:
            fallback = "抱歉，测试数据集知识库暂未找到相关记载，请尝试描述得更具体一些（如数据集名称、条号或数据分类），或稍后再试。"
            return {
                "answer": fallback,
                "sources": sources,
                "steps": ["finalize"],
                "degraded": degraded,
            }

        messages = final_prompt.format_messages(
            question=state.get("question", ""),
            milvus_context=milvus_context,
            postgres_context=postgres_context,
            external_context=external_context,
        )
        try:
            response = await llm.ainvoke(messages, config=config)
            content = getattr(response, "content", None)
            if isinstance(content, str):
                answer = content.strip()
            else:
                answer = str(response)
        except Exception as exc:  # pragma: no cover - defensive logging
            kb_logger.error("Failed to synthesise KB answer: {}", exc)
            answer = local_context if local_context and local_context != "（无本地检索结果）" else ""
            if not answer:
                answer = "检索已完成，但当前无法生成可靠的测试数据解答。"

        if not answer:
            answer = "检索已完成，但当前无法生成可靠的测试数据解答。"

        if sources:
            sources = list(dict.fromkeys(sources))

        return {
            "answer": answer,
            "sources": sources,
            "steps": ["finalize"],
            "degraded": degraded,
        }

    return finalize
