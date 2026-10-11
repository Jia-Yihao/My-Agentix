"""KB 本地检索并发化（P1-5）。

把原本串行的 PostgreSQL / Milvus 双路检索抽成可并发、可单测的纯异步函数：

- `_postgres_search`：POST kb_ingest 服务 → rerank → 双阈值过滤（语义逐字搬移
  local_search_node.py 原 :68-141）。
- `_milvus_search`：调用 `knowledge_service.search` 走 Milvus 向量库（语义搬移
  原 :159-175）。
- `hybrid_retrieve`：编排两路——两路都被选中时 `asyncio.gather` 并发；
  单路成功/失败采用「部分成功」语义；所有被选通道均失败才抛
  `AgentError(RETRIEVAL_FAILED)`。

关键语义约定（勿混淆）：
- 通道**抛异常**才算失败；**成功返回空列表不算失败**。
- 部分失败 → 返回成功通道结果并置 `degraded=True`。
"""

import asyncio
from typing import Any, Dict, List

import aiohttp

from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_states import (
    KBWorkflowConfig,
)
from agentix.config import settings
from agentix.domain.errors import AgentError, ErrorCode
# P0-2(a)：重试策略上提到 domain.policies，application 侧可直接使用（不再产生
# application → infrastructure 的新违例，见 tests/test_p17_domain_layer.py）。
from agentix.domain.policies import aretry
from agentix.domain.policies.ingest_auth import ingest_auth_headers_for
from agentix.infrastructure.core.logger import get_logger
from agentix.application.services.knowledge_gateway import KnowledgeGateway

kb_logger = get_logger(service="kb-retrieval")


async def _record_rag_retrieval(
    source: str, result_count: int, latency_ms: float
) -> bool:
    """检索埋点（P0-2(c) 接线；R-P0-5 打点不吞错：失败显式透出）。

    Redis 不可用时**绝不**影响检索主链路（当前部署确实可能无 Redis）；
    延迟导入避免加重模块导入链（与 chat_service.py 的既有做法一致）。

    R-P0-5：失败不再静默吞掉——
    - ``kb_logger.warning`` 留痕（此前 debug 级等价于吞掉）；
    - best-effort 打 ``record_degraded`` 计数（/metrics 抓取面亮
      ``degraded_total{component=rag_retrieval_*}`` 信号）；
    - 返回 ``False``（True = 记账成功；False = 埋点降级），
      供调用方按需透出——但**永不抛出**（打点失败 ≠ 检索失败，
      通道 degraded 位仍只由通道失败驱动，语义不混淆）。
    """
    try:
        from agentix.application.services.observability import get_observability

        await get_observability().record_rag_retrieval(
            source=source,
            result_count=result_count,
            latency_ms=latency_ms,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - 埋点失败绝不影响主链路
        kb_logger.warning(
            "record_rag_retrieval telemetry degraded (R-P0-5, source={}): {}",
            source,
            exc,
        )
        try:
            from agentix.observability.metrics import record_degraded

            record_degraded(f"rag_retrieval_{source}")
        except Exception:  # noqa: BLE001 - 计数上报失败不再二次抛出
            kb_logger.debug("record_degraded counter also failed (non-fatal)")
        return False


@aretry(
    max_attempts=settings.KB_RETRIEVAL_MAX_ATTEMPTS,
    base_delay=settings.KB_RETRIEVAL_RETRY_BASE_DELAY,
)
async def _postgres_search(
    question: str,
    *,
    cfg: KBWorkflowConfig,
    knowledge_service: KnowledgeGateway,
) -> List[Dict[str, Any]]:
    """查询 kb_ingest 结构化知识库（R3.7 起后端为 Milvus legal 集合）。

    POST 到 `cfg.postgres_search_url`（kb_ingest 服务，通道名沿用 "postgres"），对返回结果做 rerank
    与双阈值过滤（similarity + rerank_score），最后截断到 top_k。

    **P0-2(a) 重试接线**：本函数被 `@aretry` 包裹——检索是**只读 POST**，重试
    安全（无副作用）。重试只对 `is_retryable` 判定的瞬时故障（连接/超时/5xx）
    生效；鉴权/参数错误（4xx）立即抛出，不重试。

    **异常语义（P1-5 验收 3 的前提，重试不改变该语义）**：
    - 传输层失败（`aiohttp.ClientError` / `asyncio.TimeoutError` / OSError）、
      **HTTP 非 200**、结果后处理异常（rerank / 字段解析）→ 记录日志后
      **向上抛**，由 `hybrid_retrieve` 判定为「通道失败」（服务不可用 ≠ 查不到）；
      重试耗尽后仍是「通道失败」（行为不变）。
    - HTTP 200 但 `results` 结构不符预期 → 记 warning 并返回 `[]`——服务是活的，
      属「空结果」而非失败。
    """
    # P0-2(c)：检索埋点计时（延迟导入见 _record_rag_retrieval）
    import time as _time

    t0 = _time.perf_counter()
    postgres_results: List[Dict[str, Any]] = []
    effective_top_k = cfg.top_k
    postgres_search_url = cfg.postgres_search_url
    request_timeout = cfg.request_timeout

    kb_logger.info("🔍 [优先] 查询结构化法律知识库（kb_ingest/Milvus legal）...")
    payload: Dict[str, Any] = {
        "query": question,
        "top_k": effective_top_k,
    }
    if settings.KB_POSTGRES_SIMILARITY_THRESHOLD is not None:
        payload["threshold"] = settings.KB_POSTGRES_SIMILARITY_THRESHOLD
    try:
        timeout_cfg = aiohttp.ClientTimeout(total=request_timeout)
        async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
            async with session.post(
                postgres_search_url,
                json=payload,
                # 该 URL 通常就是本仓 kb_ingest 的 /api/search，开启鉴权后会要求
                # X-API-Key；若被运维改成第三方服务则**不附带**本仓密钥。
                headers=ingest_auth_headers_for(
                    postgres_search_url,
                    api_key=settings.KB_API_KEY,
                    ingest_service_url=settings.INGEST_SERVICE_URL,
                ),
            ) as response:
                if response.status != 200:
                    # 服务活着但报错 = 通道不可用（区别于「查不到」）
                    error_text = await response.text()
                    kb_logger.warning(
                        "PostgreSQL KB search failed ({}): {}",
                        response.status,
                        error_text,
                    )
                    raise RuntimeError(
                        f"PostgreSQL KB search returned HTTP {response.status}"
                    )
                body = await response.json()
                data_results = body.get("results") or []
                if not isinstance(data_results, list):
                    # 服务正常但数据结构不符预期 → 空结果（非通道失败）
                    kb_logger.warning(
                        "Unexpected PostgreSQL search payload structure: {}",
                        body,
                    )
                    # P0-2(c)：空结果也是"成功返回"，照常埋点
                    # （R-P0-5：失败 warning + degraded 计数透出，不吞）
                    await _record_rag_retrieval(
                        "postgres", 0, (_time.perf_counter() - t0) * 1000
                    )
                    return []
                for idx, item in enumerate(data_results):
                    item_copy = dict(item)
                    metadata_copy = dict(item_copy.get("metadata") or {})
                    item_copy["metadata"] = metadata_copy
                    item_copy["tool"] = "postgres"
                    similarity = (
                        item_copy.get("similarity")
                        if item_copy.get("similarity") is not None
                        else item_copy.get("score")
                    )
                    if similarity is not None:
                        try:
                            item_copy["similarity"] = float(similarity)
                        except (TypeError, ValueError):
                            item_copy["similarity"] = 0.0
                    item_copy["id"] = str(
                        item_copy.get("id")
                        or item_copy.get("document_id")
                        or item_copy.get("source_id")
                        or f"postgres_{idx}"
                    )
                    postgres_results.append(item_copy)
                if postgres_results and knowledge_service.reranker.enabled:
                    postgres_results = await knowledge_service.reranker.rerank(
                        question, postgres_results, effective_top_k
                    )
                filtered_postgres: List[Dict[str, Any]] = []
                for doc in postgres_results:
                    similarity = float(doc.get("similarity") or doc.get("score") or 0.0)
                    rerank_score = float(doc.get("rerank_score") or 0.0)
                    if knowledge_service.reranker.enabled:
                        if (
                            similarity >= settings.KB_POSTGRES_SIMILARITY_THRESHOLD
                            and rerank_score >= settings.KB_POSTGRES_RERANK_THRESHOLD
                        ):
                            filtered_postgres.append(doc)
                    else:
                        if similarity >= settings.KB_POSTGRES_SIMILARITY_THRESHOLD:
                            filtered_postgres.append(doc)
                postgres_results = filtered_postgres[:effective_top_k]
                kb_logger.info(
                    "✅ PostgreSQL 返回 {} 条结果，过滤后保留 {} 条",
                    len(data_results),
                    len(postgres_results),
                )
    except Exception as exc:
        # 传输层 / 后处理异常：记日志后向上抛，交由 hybrid_retrieve 记「通道失败」
        kb_logger.error("PostgreSQL knowledge search error: {}", exc)
        raise
    # P0-2(c)：检索成功埋点（R-P0-5：失败 warning + degraded 计数透出，不吞）
    await _record_rag_retrieval(
        "postgres", len(postgres_results), (_time.perf_counter() - t0) * 1000
    )
    return postgres_results


@aretry(
    max_attempts=settings.KB_RETRIEVAL_MAX_ATTEMPTS,
    base_delay=settings.KB_RETRIEVAL_RETRY_BASE_DELAY,
)
async def _milvus_search(
    question: str,
    *,
    cfg: KBWorkflowConfig,
    knowledge_service: KnowledgeGateway,
) -> List[Dict[str, Any]]:
    """查询 Milvus 向量库。

    调用 `knowledge_service.search`，为每条结果补齐 metadata 副本并打上
    `tool="milvus"` 标签。语义与原 local_search_node.py:159-175 保持一致；
    异常不再在函数内吞掉，交由 `hybrid_retrieve` 统一做「通道失败」判定。

    **P0-2(a) 重试接线**：本函数被 `@aretry` 包裹——向量检索是**只读查询**，
    重试安全。仅对瞬时故障（连接/超时/5xx）重试；重试耗尽后异常仍向上抛，
    通道失败语义（P1-5）不变。
    """
    # P0-2(c)：检索埋点计时（延迟导入见 _record_rag_retrieval）
    import time as _time

    t0 = _time.perf_counter()
    milvus_results: List[Dict[str, Any]] = []
    effective_top_k = cfg.top_k
    filter_expr = cfg.filter_expr

    docs = await knowledge_service.search(
        query=question,
        top_k=effective_top_k,
        similarity_threshold=settings.KB_SIMILARITY_THRESHOLD,
        filter_expr=filter_expr,
        filter_by_similarity=not knowledge_service.reranker.enabled,
    )
    for doc in docs:
        doc_copy = dict(doc)
        metadata_copy = dict(doc.get("metadata") or {})
        doc_copy["metadata"] = metadata_copy
        doc_copy["tool"] = "milvus"
        milvus_results.append(doc_copy)
    kb_logger.info("✅ Milvus 兜底返回 {} 条结果", len(milvus_results))
    # P0-2(c)：检索成功埋点（R-P0-5：失败 warning + degraded 计数透出，不吞）
    await _record_rag_retrieval(
        "milvus", len(milvus_results), (_time.perf_counter() - t0) * 1000
    )
    return milvus_results


async def hybrid_retrieve(
    question: str,
    *,
    cfg: KBWorkflowConfig,
    knowledge_service: KnowledgeGateway,
    tools: List[str],
) -> Dict[str, Any]:
    """并发编排 PostgreSQL / Milvus 两路本地检索。

    选择逻辑：
    - `want_postgres = "postgres" in tools and bool(cfg.postgres_search_url)`
    - `want_milvus = "milvus" in tools`；若选了 postgres 但未配置
      `postgres_search_url` → 记 warning 并强制 `want_milvus = True`
      （保留原 :61-66 的降级行为）。
    - 两路都 want → `asyncio.gather(..., return_exceptions=True)` 并发。
    - 只有一路 want → 直接 await 该路（不启用 gather）。
    - 一路都不 want → 记 warning 并返回空结果。

    失败判定：某通道抛异常（或 gather 返回 BaseException）记入 `failures`；
    成功返回空列表**不算**失败。所有被选通道均在 `failures` 中 → 抛
    `AgentError(ErrorCode.RETRIEVAL_FAILED)`。

    Returns:
        形如 `{"postgres": [...], "milvus": [...], "failures": [...], "degraded": bool}`。
    """
    tools = list(tools or [])

    want_postgres = "postgres" in tools and bool(cfg.postgres_search_url)
    want_milvus = "milvus" in tools
    if "postgres" in tools and not cfg.postgres_search_url:
        kb_logger.warning(
            "PostgreSQL 工具被选中，但 INGEST_SERVICE_URL 未配置，跳过 PostgreSQL 直接使用 Milvus。"
        )
        want_milvus = True

    wanted: List[str] = []
    if want_postgres:
        wanted.append("postgres")
    if want_milvus:
        wanted.append("milvus")

    if not wanted:
        kb_logger.warning("⚠️ 未选择任何可用的知识库工具")
        return {"postgres": [], "milvus": [], "failures": [], "degraded": False}

    postgres_results: List[Dict[str, Any]] = []
    milvus_results: List[Dict[str, Any]] = []
    failures: List[str] = []

    if want_postgres and want_milvus:
        # 两路无数据依赖 → 并发下发，单路失败不拖垮整体
        outcome = await asyncio.gather(
            _postgres_search(question, cfg=cfg, knowledge_service=knowledge_service),
            _milvus_search(question, cfg=cfg, knowledge_service=knowledge_service),
            return_exceptions=True,
        )
        pg_outcome, milvus_outcome = outcome[0], outcome[1]
        if isinstance(pg_outcome, BaseException):
            failures.append("postgres")
            kb_logger.warning("PostgreSQL 检索通道失败: {}", pg_outcome)
        else:
            postgres_results = pg_outcome
        if isinstance(milvus_outcome, BaseException):
            failures.append("milvus")
            kb_logger.warning("Milvus 检索通道失败: {}", milvus_outcome)
        else:
            milvus_results = milvus_outcome
    elif want_postgres:
        try:
            postgres_results = await _postgres_search(
                question, cfg=cfg, knowledge_service=knowledge_service
            )
        except Exception as exc:  # pragma: no cover - defensive
            failures.append("postgres")
            kb_logger.warning("PostgreSQL 检索通道失败: {}", exc)
    else:  # want_milvus
        try:
            milvus_results = await _milvus_search(
                question, cfg=cfg, knowledge_service=knowledge_service
            )
        except Exception as exc:  # pragma: no cover - defensive
            failures.append("milvus")
            kb_logger.warning("Milvus 检索通道失败: {}", exc)

    if failures and len(failures) == len(wanted):
        raise AgentError(ErrorCode.RETRIEVAL_FAILED, "所有检索通道均失败")

    return {
        "postgres": postgres_results,
        "milvus": milvus_results,
        "failures": failures,
        "degraded": bool(failures),
    }
