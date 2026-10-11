"""
LangGraph node for knowledge base powered responses.
"""
from typing import Any, Callable, Coroutine, Dict, List, Optional

try:  # pragma: no cover - prefer typing_extensions for Pydantic compatibility
    from typing_extensions import TypedDict  # type: ignore
except ImportError:  # pragma: no cover - minimal fallback
    from typing import TypedDict

from agentix.config import settings
from agentix.infrastructure.core.logger import get_logger
from agentix.domain.ports.retrieval import RetrievalPort
from agentix.application.services.knowledge_gateway import get_retrieval_port
from agentix.application.services.llm_client import LLMClient
from agentix.tools.gateway.kg_gateway import run_governed
from .prompts import build_knowledge_system_prompt
from agentix.tools.adapters.search import SearchTool

logger = get_logger(service="kg-knowledge-agent")


class KnowledgeQueryInputState(TypedDict, total=False):
    """Expected input for the knowledge base node."""

    task: str
    context: Dict[str, Any]
    steps: List[str]


class KnowledgeQueryOutputState(TypedDict):
    """Output payload produced by the knowledge base node."""

    answer: str
    type: str
    sources: List[str]
    confidence: float
    metadata: Dict[str, Any]
    steps: List[str]


FALLBACK_MESSAGE = "抱歉，我在知识库中没有找到相关信息。您可以换个方式提问吗？"


def _build_context_snippet(documents: List[Dict[str, Any]], limit: int = 5) -> str:
    # 企业化 P1-4.2 ①：文档内容定界包裹（数据 ≠ 指令），防检索内容挟持 Agent
    from agentix.config.settings import settings as main_settings
    from agentix.security.prompt_injection import wrap_document_content

    snippets: List[str] = []
    for idx, doc in enumerate(documents[:limit]):
        content = doc.get("content") or doc.get("document") or ""
        meta = doc.get("metadata") or {}
        source = str(doc.get("source") or meta.get("source") or f"文档{idx + 1}")
        if main_settings.PROMPT_INJECTION_DETECTION_ENABLED:
            snippets.append(wrap_document_content(content, source=source))
        else:
            snippets.append(f"文档 {idx + 1}:\n{content}")
    return "\n\n".join(snippets)


def _calculate_confidence(documents: List[Dict[str, Any]]) -> float:
    if not documents:
        return 0.0
    scores = [doc.get("score", 0.0) for doc in documents if isinstance(doc.get("score", 0.0), (int, float))]
    if not scores:
        return 0.0
    avg_score = sum(scores) / len(scores)
    return float(min(max(avg_score, 0.0), 1.0))


def _collect_sources(documents: List[Dict[str, Any]]) -> List[str]:
    sources: List[str] = []
    for doc in documents:
        meta = doc.get("metadata") or {}
        candidate = doc.get("source") or meta.get("source") or meta.get("url") or meta.get("id")
        if candidate:
            sources.append(str(candidate))
    return sources


def create_knowledge_query_node(
    retrieval_port: Optional[RetrievalPort] = None,
    llm_client: Optional[LLMClient] = None,
) -> Callable[[KnowledgeQueryInputState], Coroutine[Any, Any, KnowledgeQueryOutputState]]:
    """
    Build a LangGraph node that queries the legal knowledge base and crafts an answer.
    """

    retrieval_port = retrieval_port or get_retrieval_port()

    # Lazily construct a client only if API key is present.
    llm_client = llm_client or (
        LLMClient() if settings.OPENAI_API_KEY else None
    )

    async def knowledge_query(state: KnowledgeQueryInputState) -> KnowledgeQueryOutputState:
        question = state.get("task") or ""
        context = state.get("context") or {}
        prior_steps = list(state.get("steps", []))

        if not question:
            logger.warning("Knowledge node invoked without a question payload.")
            return KnowledgeQueryOutputState(
                answer=FALLBACK_MESSAGE,
                type="knowledge",
                sources=[],
                confidence=0.0,
                metadata={"reason": "missing_question"},
                steps=prior_steps + ["knowledge_query"],
            )

        top_k = context.get("top_k")
        similarity_threshold = context.get("similarity_threshold")
        filter_expr = context.get("filter_expr")
        kb_id = context.get("kb_id", "default")
        tenant_id = context.get("tenant_id", "default")

        try:
            result = await retrieval_port.search(
                query=question,
                kb_id=kb_id,
                tenant_id=tenant_id,
                top_k=top_k or 5,
            )
            if result["status"] == "degraded":
                logger.warning(
                    "Knowledge search degraded: %s", result.get("error")
                )
                documents = []
            else:
                documents = [
                    {
                        "content": chunk["content"],
                        "score": chunk["score"],
                        "metadata": {
                            "chunk_id": chunk["chunk_id"],
                            "document_id": chunk["citation"]["doc_id"],
                            "source": chunk["citation"]["doc_title"],
                            "page": chunk["citation"]["page"],
                            "id": chunk["citation"]["doc_id"],
                        },
                        "source": chunk["citation"]["doc_title"],
                        "id": chunk["citation"]["doc_id"],
                    }
                    for chunk in result["chunks"]
                ]
            logger.info("Knowledge search retrieved {} documents.", len(documents))
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Knowledge search failed: {}", exc)
            return KnowledgeQueryOutputState(
                answer=FALLBACK_MESSAGE,
                type="knowledge",
                sources=[],
                confidence=0.0,
                metadata={"reason": "search_failure", "error": str(exc)},
                steps=prior_steps + ["knowledge_query"],
            )

        if not documents:
            return KnowledgeQueryOutputState(
                answer=FALLBACK_MESSAGE,
                type="knowledge",
                sources=[],
                confidence=0.0,
                metadata={"reason": "no_documents"},
                steps=prior_steps + ["knowledge_query"],
            )

        web_results: List[Dict[str, Any]] = []
        if settings.KB_ENABLE_EXTERNAL_SEARCH:
            try:
                search_tool = SearchTool()
                web_top_k = context.get("web_top_k")
                web_results = await run_governed(
                    "search",
                    {"query": question, "num_results": web_top_k},
                    direct=lambda: search_tool.search(question, num_results=web_top_k),
                )
                logger.info("External search retrieved %s results.", len(web_results))
            except RuntimeError as exc:
                logger.warning("External search disabled or misconfigured: {}", exc)
            except Exception as exc:
                logger.error("External search failed: {}", exc)

        context_snippet = _build_context_snippet(documents)
        if web_results:
            web_snippets: List[str] = []
            for idx, item in enumerate(web_results[:5]):
                title = item.get("title") or ""
                url = item.get("url") or ""
                snippet = item.get("snippet") or ""
                web_snippets.append(
                    f"搜索结果 {idx + 1}：{title}\n链接：{url}\n摘要：{snippet}"
                )
            web_context = "\n\n".join(web_snippets)
            context_snippet = (
                f"{context_snippet}\n\n外部搜索结果：\n{web_context}"
                if context_snippet
                else f"外部搜索结果：\n{web_context}"
            )

        sources = _collect_sources(documents)
        if web_results:
            sources.extend(
                item.get("url", "")
                for item in web_results
                if item.get("url")
            )
        confidence = _calculate_confidence(documents)

        system_prompt = build_knowledge_system_prompt(context_snippet)

        metadata: Dict[str, Any] = {
            "documents": [
                {
                    "id": doc.get("id") or doc.get("metadata", {}).get("id"),
                    "score": doc.get("score"),
                    "source": doc.get("source") or doc.get("metadata", {}).get("source"),
                }
                for doc in documents[:5]
            ],
            "top_k": top_k,
            "similarity_threshold": similarity_threshold,
            "filter_expr": filter_expr,
            "web_results": web_results,
        }

        if llm_client is None:
            logger.warning("LLM client unavailable; returning top document content.")
            top_content = documents[0].get("content") or documents[0].get("document") or FALLBACK_MESSAGE
            metadata["reason"] = "llm_unavailable"
            return KnowledgeQueryOutputState(
                answer=str(top_content),
                type="knowledge",
                sources=sources,
                confidence=confidence,
                metadata=metadata,
                steps=prior_steps + ["knowledge_query"],
            )

        try:
            answer = await llm_client.chat(
                system_prompt=system_prompt,
                user_message=question,
                temperature=0.2,
            )
            metadata["reason"] = "success"
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Knowledge answer generation failed: {}", exc)
            metadata["reason"] = "llm_failure"
            metadata["error"] = str(exc)
            answer = documents[0].get("content") or documents[0].get("document") or FALLBACK_MESSAGE

        final_answer = answer.strip() or FALLBACK_MESSAGE

        return KnowledgeQueryOutputState(
            answer=final_answer,
            type="knowledge",
            sources=sources,
            confidence=confidence,
            metadata=metadata,
            steps=prior_steps + ["knowledge_query"],
        )

    return knowledge_query
