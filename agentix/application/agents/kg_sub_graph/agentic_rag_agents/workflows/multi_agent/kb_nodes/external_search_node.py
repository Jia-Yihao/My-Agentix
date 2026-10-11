"""KB external_search 节点（P1-1 拆分，从 multi_tool.py 搬移）。"""

from typing import Any, Dict, List

import aiohttp

from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_states import (
    KBWorkflowConfig,
    KBWorkflowState,
)
from agentix.config import settings
from agentix.domain.policies.ingest_auth import ingest_auth_headers_for
from agentix.infrastructure.core.logger import get_logger

kb_logger = get_logger(service="kb-external-search-node")


def create_external_search_node(cfg: KBWorkflowConfig):
    """构造 external_search 节点：调用外部检索 API（未启用/同源时跳过）。"""

    async def external_search(state: KBWorkflowState) -> Dict[str, Any]:
        if not (cfg.allow_external_search and cfg.external_url):
            return {"external_results": [], "steps": ["external_search"]}

        if cfg.external_is_postgres and "postgres" in (state.get("kb_tools") or []):
            kb_logger.debug(
                "Skip external search: router already执行了 PostgreSQL 工具，且外部检索与其同源。"
            )
            return {"external_results": [], "steps": ["external_search"]}

        question = state.get("question", "")
        if not question.strip():
            return {"external_results": [], "steps": ["external_search"]}

        payload: Dict[str, Any] = {
            "query": question,
            "top_k": cfg.top_k,
        }
        if cfg.similarity_threshold is not None:
            payload["threshold"] = cfg.similarity_threshold

        results: List[Dict[str, Any]] = []
        try:
            timeout_cfg = aiohttp.ClientTimeout(total=cfg.request_timeout)
            async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
                async with session.post(
                    cfg.external_url,
                    json=payload,
                    # 目标可能是本仓 kb_ingest（会要求 X-API-Key），
                    # 也可能是运维另配的第三方检索服务——后者**不能**收到本仓密钥。
                    # 因此按 origin 判断，只在确实是 kb_ingest 时才附带。
                    headers=ingest_auth_headers_for(
                        cfg.external_url,
                        api_key=settings.KB_API_KEY,
                        ingest_service_url=settings.INGEST_SERVICE_URL,
                    ),
                ) as response:
                    if response.status == 200:
                        body = await response.json()
                        data_results = body.get("results") or []
                        if isinstance(data_results, list):
                            results = data_results
                        else:
                            kb_logger.warning(
                                "Unexpected external search payload structure: {}",
                                body,
                            )
                    else:
                        error_text = await response.text()
                        kb_logger.warning(
                            "External KB search failed ({}): {}",
                            response.status,
                            error_text,
                        )
        except Exception as exc:  # pragma: no cover - defensive logging
            kb_logger.error("External KB search error: {}", exc)

        return {
            "external_results": results,
            "steps": ["external_search"],
        }

    return external_search
