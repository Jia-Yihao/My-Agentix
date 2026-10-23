"""KB 多工具工作流装配（P1-1 拆分）。

仅保留配置解析、节点工厂调用与图装配；
节点实现见 kb_nodes/，Prompt 见 prompts/kb_prompts.py，格式化纯函数见 kb_formatters.py。
"""

from typing import Optional

from langchain_core.language_models import BaseChatModel
from langgraph.constants import END, START
from langgraph.graph.state import CompiledStateGraph, StateGraph

from agentix.config import settings
from agentix.infrastructure.core.logger import get_logger
from agentix.application.services.knowledge_gateway import KnowledgeGateway

from .kb_nodes import (
    create_external_search_node,
    create_finalize_node,
    create_guardrails_node,
    create_local_search_node,
    create_router_node,
)
from .kb_states import (
    KBInputState,
    KBOutputState,
    KBWorkflowConfig,
    KBWorkflowState,
)

kb_logger = get_logger(service="kb-multi-tool")


def create_kb_multi_tool_workflow(
    llm: BaseChatModel,
    knowledge_service: Optional[KnowledgeGateway] = None,
    *,
    top_k: Optional[int] = None,
    similarity_threshold: Optional[float] = None,
    filter_expr: Optional[str] = None,
    allow_external: Optional[bool] = None,
    external_search_url: Optional[str] = None,
    external_search_timeout: Optional[float] = None,
    scope_description: Optional[str] = None,
) -> CompiledStateGraph:
    """
    Create a multi-tool workflow for knowledge base queries.

    This workflow performs guardrails checking, routes the question to the most
    appropriate retrieval source (local vector store, external API, or both),
    and then synthesises a response with safety-aware instructions.
    """

    knowledge_service = knowledge_service or KnowledgeGateway()
    effective_top_k = top_k or settings.KB_TOP_K
    effective_threshold = (
        similarity_threshold
        if similarity_threshold is not None
        else settings.KB_SIMILARITY_THRESHOLD
    )

    allow_external_search = (
        allow_external
        if allow_external is not None
        else settings.KB_ENABLE_EXTERNAL_SEARCH
    )

    ingest_service_base = settings.INGEST_SERVICE_URL.rstrip("/") if settings.INGEST_SERVICE_URL else None

    postgres_search_url = (
        f"{ingest_service_base}/api/v1/knowledge/search" if ingest_service_base else None
    )

    external_url = external_search_url or settings.KB_EXTERNAL_SEARCH_URL

    if allow_external_search and not external_url:
        kb_logger.warning(
            "External search enabled but KB_EXTERNAL_SEARCH_URL 未配置，已自动关闭外部检索。"
        )
        allow_external_search = False

    external_is_postgres = bool(
        allow_external_search
        and external_url
        and postgres_search_url
        and external_url.rstrip("/") == postgres_search_url.rstrip("/")
    )

    if not allow_external_search:
        external_url = None

    request_timeout = (
        external_search_timeout
        if external_search_timeout is not None
        else settings.KB_EXTERNAL_SEARCH_TIMEOUT
    )

    scope_text = scope_description or (
        "测试数据集知识库仅处理数据条目释义、数据来源背景与沿革、数据规则与学说、典型案例与示例要点解读，"
        "以及数据集、数据分类、示例说明、机构等结构化数据信息的查询。"
        "不包含具体个案代理、政治敏感、医疗诊断、隐私或其他未授权主题相关的问题。"
    )
    config = KBWorkflowConfig(
        top_k=effective_top_k,
        similarity_threshold=effective_threshold,
        postgres_search_url=postgres_search_url,
        external_url=external_url,
        allow_external_search=allow_external_search,
        external_is_postgres=external_is_postgres,
        request_timeout=request_timeout,
        filter_expr=filter_expr,
        scope_text=scope_text,
    )

    # P1-1 拆分：节点实现外置到 kb_nodes/，本函数只负责依赖注入与图装配
    guardrails = create_guardrails_node(llm, scope_text=scope_text)
    router = create_router_node(llm, allow_external_search=allow_external_search)
    local_search = create_local_search_node(knowledge_service, config)
    external_search = create_external_search_node(config)
    finalize = create_finalize_node(llm, top_k=effective_top_k)

    def guardrails_router(state: KBWorkflowState) -> str:
        return "finalize" if state.get("guardrails_decision") == "end" else "kb_router"

    def router_edge(state: KBWorkflowState) -> str:
        return "external_search" if state.get("route") == "external" else "local_search"

    def local_edge(state: KBWorkflowState) -> str:
        route = state.get("route", "local")
        if route in {"hybrid", "external"} and allow_external_search and external_url:
            return "external_search"
        return "finalize"

    graph_builder = StateGraph(
        KBWorkflowState,
        input=KBInputState,
        output=KBOutputState,
    )

    graph_builder.add_node("guardrails", guardrails)
    graph_builder.add_node("kb_router", router)
    graph_builder.add_node("local_search", local_search)
    graph_builder.add_node("external_search", external_search)
    graph_builder.add_node("finalize", finalize)

    graph_builder.add_edge(START, "guardrails")
    graph_builder.add_conditional_edges("guardrails", guardrails_router)
    graph_builder.add_conditional_edges("kb_router", router_edge)
    graph_builder.add_conditional_edges("local_search", local_edge)
    graph_builder.add_edge("external_search", "finalize")
    graph_builder.add_edge("finalize", END)

    return graph_builder.compile()
