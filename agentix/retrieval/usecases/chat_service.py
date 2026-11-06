"""
对话问答业务编排（Agent + 工具）

流程：
1. 验证 KB / 索引 / 会话范围
2. 保存用户消息并返回 meta
3. 内部工具循环按问题选用工具（检索 / 统计 / 列表 / 关键词搜索）
4. 流式输出最终回答并保存引用
"""
import json
import asyncio
import hashlib
import logging
import re
import sys
import time
from typing import AsyncGenerator

import redis
from sqlmodel import Session

from agentix.retrieval.api.response import ErrorCode, ERROR_CODE_TO_HINT
from agentix.retrieval.api.authz import require_kb_owned
from agentix.retrieval.errors import AppError
from agentix.retrieval.config import get_default_config
from agentix.retrieval.usecases.chat_llm import ChatMode, create_chat_llm, resolve_chat_model
from agentix.retrieval.usecases.chat_scope import count_documents_in_scope
from agentix.retrieval.domain.models.chat import Conversation, Message
from agentix.retrieval.domain.models.index import Answer, RetrievalRun
from agentix.retrieval.infra.repos.index_repo import get_active_index_artifact
from agentix.retrieval.settings import settings
from agentix.observability.spans import span
from agentix.observability.metrics import (
    record_retry_attempt,
    record_retry_exhausted,
    record_retry_sleep,
)
from agentix.retrieval.infra.repos.conversation_repo import (
    get_conversation_by_id,
    create_conversation,
)

logger = logging.getLogger(__name__)

# 检索未过阈值 / 无命中时的统一拒答（工具与 Agent 预检共用）
INSUFFICIENT_RETRIEVAL_MSG = "未检索到足够相关内容，无法基于文档回答。"


def _persist_assistant_reply(
    session: Session,
    *,
    conversation_id: str,
    question_message_id: str,
    answer_text: str,
    model_name: str,
) -> tuple[Answer, Message]:
    """保存助手回复（含失败提示），便于会话历史回放。"""
    answer = Answer(
        conversation_id=conversation_id,
        question_message_id=question_message_id,
        version=1,
        is_current=True,
        answer_text=answer_text,
        citations=[],
        model_name=model_name,
    )
    session.add(answer)
    session.commit()
    session.refresh(answer)
    assistant_msg = Message(
        conversation_id=conversation_id,
        role="assistant",
        content=answer_text,
        answer_id=answer.id,
    )
    session.add(assistant_msg)
    session.commit()
    session.refresh(assistant_msg)
    return answer, assistant_msg


def _finished_answer_chunk(
    session: Session,
    *,
    conversation_id: str,
    question_message_id: str,
    answer_text: str,
    model_name: str,
    request_id: str,
) -> dict:
    answer, assistant_msg = _persist_assistant_reply(
        session,
        conversation_id=conversation_id,
        question_message_id=question_message_id,
        answer_text=answer_text,
        model_name=model_name,
    )
    return {
        "type": "answer_chunk",
        "content": answer_text,
        "request_id": request_id,
        "finished": True,
        "conversation_id": conversation_id,
        "message_id": assistant_msg.id,
        "answer_id": answer.id,
        "citations": [],
    }

_WS_RE = re.compile(r"\s+")
_META_PREFLIGHT_SKIP_RE = re.compile(
    r"(几个|多少|有哪些|列出|列表).{0,12}(文件|文档)|"
    r"(文件|文档).{0,8}(几个|多少|有哪些|列出|列表)"
)


def _normalize_question(q: str) -> str:
    return _WS_RE.sub(" ", (q or "").strip())


def _fusion_fingerprint() -> str:
    """W2 缓存键升级：融合/裁剪配置指纹（调参或翻融合开关 ⇒ 不读旧池）。

    只取**影响结果集合**的融合旋钮（方法/k/alpha/池上限/sparse 裁剪）；
    归一化摘要 8 位十六进制，键长可控。改这些参数 ⇒ 指纹变 ⇒ 旧缓存自然
    不命中（无需手动清池）。
    """
    parts = "|".join(
        str(getattr(settings, name, ""))
        for name in (
            "RETRIEVAL_FUSION_METHOD",
            "RETRIEVAL_RRF_K",
            "RETRIEVAL_FUSION_ALPHA",
            "RETRIEVAL_FUSION_TOP_N",
            "RETRIEVAL_SPARSE_MIN_SCORE",
        )
    )
    return hashlib.sha1(  # nosec B324 —— 非安全用途：缓存键配置指纹
        parts.encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:8]


def _resolve_cache_tenant(session, kb_id: str) -> str:
    """W2 缓存键升级：租户分池（同 kb 不同租户绝不共池）。

    主键查 KnowledgeBase.tenant_id；任何失败（无 session / 非法 kb /
    存储不可用）一律 fail-open 回 ``"default"``——查不到租户只可能让
    键落进默认池（同租户自洽），绝不让租户解析失败打挂检索主链路。
    """
    if session is None:
        return "default"
    try:
        from agentix.retrieval.infra.repos.kb_repo import get_kb_by_id

        kb = get_kb_by_id(session, kb_id)
        tid = str(getattr(kb, "tenant_id", "") or "").strip()
        return tid or "default"
    except Exception:  # noqa: BLE001 - 租户解析 fail-open，绝不打挂检索
        return "default"


def _retrieval_cache_key(
    *,
    kb_id: str,
    artifact_id: str,
    search_k: int,
    question_norm: str | None,
    scope_token: str,
    tenant_id: str = "default",
) -> str:
    """缓存键（W2 升级）：租户 + 融合指纹 + ``|k2`` 版本标记。

    版本标记放在基础段**尾部**（调用方的 ``|h1``/``|r1``/``|y1`` 追加在
    其后仍居末位，既有 endswith 断言不受影响）；旧键（无 ``|k2``）自然
    冷却，属计划内一次性失效。

    E7：``question_norm=None`` ⇒ 产出**不含问法**的作用域段（摘要位写成字面量 ``sem``）。
    语义缓存的隔离范围必须由这个函数与精确键**共同**决定——自己拼一份"租户+kb+权限"
    字符串就是给权限隔离造第二个源（约定 6 ④），而第二源出错的方式是跨权限命中。
    字面量 ``sem`` 与真实摘要（24 位十六进制）不可能相撞 ⇒ 精确键与语义作用域互不污染。
    """
    digest = (
        "sem"
        if question_norm is None
        else hashlib.sha256(question_norm.encode("utf-8")).hexdigest()[:24]
    )
    prefix = (getattr(settings, "CACHE_REDIS_PREFIX", "agentix_retrieval:retrieve:") or "agentix_retrieval:retrieve:").strip()
    return (
        f"{prefix}{tenant_id}:{kb_id}:{artifact_id}:{search_k}:"
        f"{scope_token}:{digest}|{_fusion_fingerprint()}|k2"
    )


def _serialize_docs_for_cache(docs: list) -> str:
    return json.dumps(
        [{"page_content": d.page_content, "metadata": (d.metadata or {})} for d in docs],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _deserialize_docs_from_cache(raw: bytes):
    from langchain_core.documents import Document as LCDocument

    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, list):
        return None
    docs: list[LCDocument] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        page_content = item.get("page_content")
        metadata = item.get("metadata") or {}
        if not isinstance(page_content, str) or not isinstance(metadata, dict):
            continue
        docs.append(LCDocument(page_content=page_content, metadata=metadata))
    return docs


def build_retriever(index_path: str):
    """构建向量库检索器（支持 FAISS/Milvus/Qdrant）"""
    cfg = get_default_config()
    from agentix.retrieval.infra.vectorstore.manager import create_vector_store

    backend = settings.VECTOR_STORE_BACKEND
    options = {"index_path": index_path}
    if backend == "milvus":
        options.update(
            host=settings.MILVUS_HOST,
            port=settings.MILVUS_PORT,
            collection_name=settings.MILVUS_COLLECTION,
        )
    elif backend == "qdrant":
        options.update(
            host=settings.QDRANT_HOST,
            port=settings.QDRANT_PORT,
            collection_name=settings.QDRANT_COLLECTION,
        )
    vsm = create_vector_store(backend=backend, **options)
    return vsm.get_retriever(k=cfg.get("search_k", settings.KB_TOP_K)), vsm


def retrieval_max_distance() -> float | None:
    """RETRIEVAL_MAX_DISTANCE>0 时启用 L2 距离过滤（越小越相似）。"""
    v = float(getattr(settings, "RETRIEVAL_MAX_DISTANCE", 0) or 0)
    return v if v > 0 else None


def retrieval_min_score() -> float | None:
    """M5: RETRIEVAL_MIN_SCORE>0 时启用相似度下限过滤（越大越相似）。"""
    v = float(getattr(settings, "RETRIEVAL_MIN_SCORE", 0) or 0)
    return v if v > 0 else None


def _filter_scored_pairs(pairs: list[tuple]) -> list[tuple]:
    """按阈值过滤检索结果。
    - 相似度口径（INDEX_SCORE_IS_SIMILARITY=true）：score >= RETRIEVAL_MIN_SCORE
    - 距离口径（默认）：score <= RETRIEVAL_MAX_DISTANCE
    """
    if settings.INDEX_SCORE_IS_SIMILARITY:
        min_s = retrieval_min_score()
        if min_s is None:
            return pairs
        return [(doc, score) for doc, score in pairs if float(score) >= min_s]
    max_d = retrieval_max_distance()
    if max_d is None:
        return pairs
    return [(doc, score) for doc, score in pairs if float(score) <= max_d]


def docs_from_scored_pairs(pairs: list[tuple], *, limit: int) -> list:
    out: list = []
    for doc, score in pairs[:limit]:
        meta = dict(doc.metadata or {})
        meta["_retrieval_score"] = float(score)
        doc.metadata = meta
        out.append(doc)
    return out


def hit_chunks_from_docs(docs: list) -> list[dict]:
    # R2-2 数据条目有效性过滤（指南 §2.2.7①）：sources/hit_chunks 携带有效性状态（缺失 → in_force）
    from agentix.domain.policies.legal_effectiveness import attach_effective_status

    return [
        attach_effective_status(
            {
                "chunk_id": (d.metadata or {}).get("chunk_id"),
                "score": float((d.metadata or {}).get("_retrieval_score", 0)),
                # R1-2 引用验证器 v1：内容预览与来源（供答案文本数据条目引用交叉核验）
                "content": (d.page_content or "")[:600],
                "source": str((d.metadata or {}).get("source", "")),
            },
            d.metadata or {},
        )
        for d in docs
    ]


def should_skip_retrieval_preflight(question: str) -> bool:
    """统计/列文档类问题不走检索预检，避免挡住 count/list 工具。"""
    q = _normalize_question(question)
    return bool(_META_PREFLIGHT_SKIP_RE.search(q))


def _as_page_content_doc(doc):
    """把后端返回的裸文档规整为 langchain Document 契约。

    三个真实后端（faiss/milvus/qdrant）的 ``similarity_search`` 都返回
    ``tuple[VectorDoc-like, score]``，其中 VectorDoc 只有 ``.text``，没有
    ``.page_content``；而下游（docs_from_scored_pairs / hit_chunks_from_docs /
    rerank_documents / 引用构建）一律按 langchain Document 契约访问。
    已带 ``.page_content`` 的对象原样返回（幂等）。
    """
    if hasattr(doc, "page_content"):
        return doc
    from langchain_core.documents import Document as LCDocument

    return LCDocument(
        page_content=str(getattr(doc, "text", "") or ""),
        metadata=dict(getattr(doc, "metadata", None) or {}),
    )


def _similarity_search_with_fetch(vectorstore, question, *, search_k, metadata_filter, fetch_k) -> list:
    """统一封装带 fetch_k 的向量检索，返回已按距离阈值过滤、截断到 search_k 的文档列表。

    R2-2（指南 §2.2.7①）：数据条目有效性后过滤先于距离阈值过滤与截断执行
    （已失效/未生效条文不进入召回；缺失有效性字段的存量数据默认有效）。
    """
    if hasattr(vectorstore, "similarity_search_with_score"):
        pairs = vectorstore.similarity_search_with_score(
            question,
            k=search_k,
            filter=metadata_filter,
            fetch_k=fetch_k,
        )
    else:
        raw = vectorstore.similarity_search(
            question,
            k=search_k,
            filter=metadata_filter,
            fetch_k=fetch_k,
        )
        pairs = []
        for item in raw or []:
            if isinstance(item, tuple) and len(item) == 2:
                doc, score = item
            else:
                doc, score = item, 0.0
            # 后端 tuple 真实分数（此前整包当 doc → AttributeError；且分数被置 0
            # 导致距离阈值/相似度阈值恒失效）——解包 + 转 Document。
            pairs.append((_as_page_content_doc(doc), float(score or 0.0)))
    from agentix.domain.policies.legal_effectiveness import filter_in_force

    pairs = filter_in_force(pairs, enabled=settings.EFFECTIVENESS_FILTER_ENABLED)
    return docs_from_scored_pairs(_filter_scored_pairs(pairs), limit=search_k)


def _resolve_retrieval_fetch_k(vectorstore, search_k: int, metadata_filter, *, post_filtered: bool = False) -> int:
    """有界 fetch_k：带 filter 时不再全库扫描，取 ``min(ntotal, search_k*M, MIN)`` 与 base 的较大者。

    - ``metadata_filter is None`` 且无后过滤时检索整库，返回 base（``search_k*4`` 与 20 的较大者）。
    - 带 filter 或开启有效性后过滤（R2-2 ``post_filtered``）时：
      ``bounded = max(search_k*MULTIPLIER, MIN)``，再 ``min(ntotal, bounded)``；
      ``MAX_FETCH_K>0`` 时进一步封顶。最终 ``max(base, bounded)`` 保证至少有 base 余量。
    """
    base = max(search_k * 4, 20)
    if metadata_filter is None and not post_filtered:
        return base
    ntotal = int(getattr(getattr(vectorstore, "index", None), "ntotal", 0) or 0)
    bounded = max(search_k * settings.RETRIEVAL_FETCH_K_MULTIPLIER, settings.RETRIEVAL_FETCH_K_MIN)
    if settings.RETRIEVAL_MAX_FETCH_K > 0:
        bounded = min(bounded, settings.RETRIEVAL_MAX_FETCH_K)
    if ntotal <= 0:
        return max(base, bounded)
    return max(base, min(ntotal, bounded))


def retrieve_docs_with_retry(
    vectorstore,
    question: str,
    *,
    search_k: int,
    metadata_filter=None,
    max_retries: int = 2,
) -> list:
    """向量检索（带分数 + 可选距离阈值）；metadata_filter 为 None 时检索整库。

    带 filter 时 ``fetch_k`` 有界（见 :func:`_resolve_retrieval_fetch_k`）。若过滤后召回不足
    ``search_k`` 且仍有扩大空间，按 4× 升级重试至多 ``RETRIEVAL_FETCH_K_MAX_ESCALATIONS``
    次，作为召回不降的保险（开关 ``RETRIEVAL_FETCH_K_ESCALATE``）。
    """
    # R2-2：有效性后过滤会缩减候选集，fetch_k 按"有过滤"口径取有界值补偿召回
    post_filtered = bool(settings.EFFECTIVENESS_FILTER_ENABLED)
    fetch_k = _resolve_retrieval_fetch_k(vectorstore, search_k, metadata_filter, post_filtered=post_filtered)
    ntotal = int(getattr(getattr(vectorstore, "index", None), "ntotal", 0) or 0)
    escalations = settings.RETRIEVAL_FETCH_K_MAX_ESCALATIONS if settings.RETRIEVAL_FETCH_K_ESCALATE else 0
    last_error = None
    gave_up_non_retryable = False
    for attempt in range(max_retries + 1):
        try:
            docs = _similarity_search_with_fetch(
                vectorstore,
                question,
                search_k=search_k,
                metadata_filter=metadata_filter,
                fetch_k=fetch_k,
            )
            # 召回不足且可扩大 fetch_k：升级重试（保召回），不消耗异常重试额度
            if escalations > 0 and ntotal > 0 and fetch_k < ntotal and len(docs) < search_k:
                fetch_k = min(ntotal, fetch_k * 4)
                escalations -= 1
                continue
            return docs
        except Exception as e:
            last_error = e
            logger.warning(
                "similarity_search_with_score failed (attempt %s/%s): %s",
                attempt + 1,
                max_retries + 1,
                e,
            )
            if attempt < max_retries:
                # 重试预算（RETRY_BUDGET_ENABLED，flag 关 = 原样重试）：
                # 分类审计闸（仅瞬时性错误可重试）+ run 级共享预算扣减。
                if bool(getattr(settings, "RETRY_BUDGET_ENABLED", False)):
                    from agentix.reliability.retry_budget import (
                        ensure_retry_budget,
                        retryable_error,
                    )

                    if not retryable_error(e):
                        logger.info(
                            "similarity search not retryable (%s): giving up",
                            type(e).__name__,
                        )
                        gave_up_non_retryable = True
                        break
                    if not ensure_retry_budget().try_consume(layer="retrieval"):
                        logger.warning(
                            "retry budget exhausted (retrieval): giving up"
                        )
                        break
                # LG-3-3：只有真的退避了才落账（预算拒批的路径上一行就 break 了）。
                sleep_s = 0.5 * (attempt + 1)
                record_retry_attempt("retrieval")
                record_retry_sleep("retrieval", sleep_s)
                time.sleep(sleep_s)
    if max_retries > 0 and not gave_up_non_retryable:
        # 重试链以失败收场：尝试用尽或被预算拒批。首次即不可重试的错误不算
        # 「重试耗尽」——它从没进入重试。
        record_retry_exhausted("retrieval")
    raise last_error


async def retrieve_scoped_docs_cached(
    *,
    kb_id: str,
    question: str,
    search_k: int,
    metadata_filter,
    vectorstore,
    cache_redis: redis.Redis | None,
    artifact_id: str,
    scope_token: str,
    session=None,
    tenant_id: str | None = None,
    history: str = "",
) -> list:
    """带 Redis 缓存的范围内向量检索（供 query_knowledge_base 工具调用）。

    R-P1（F1a）：``session`` 供 hybrid sparse 路读取 BM25 语料（可选——缺省时
    sparse 通道 fail-open 跳过；``RETRIEVAL_HYBRID_ENABLED`` 默认关，flag-off
    路径语义与接线前完全一致）。

    R-P2-1（F2b-b）：``history`` 供多轮改写做意图补全（预检路径传入；工具/端口
    适配路径缺省为空 ⇒ 单轮意图补全）。改写/HyDE 两 flag 默认关 = 基线逐位等价。

    ``tenant_id`` 供缓存键租户分池（端口适配路径显式传入；其余
    路径缺省时从 ``session`` 查 KnowledgeBase，失败 fail-open 回 "default"）；
    在飞合并/自适应级联分别由 ``RETRIEVAL_SINGLEFLIGHT_ENABLED`` /
    ``RETRIEVAL_ADAPTIVE_ENABLED`` 门控（默认关 = 与基线逐位等价）。
    """
    # The artifact id is the immutable knowledge snapshot used for this
    # retrieval. Keep it on the request trace, including cache-hit paths.
    from agentix.observability.trajectory import current_recorder

    recorder = current_recorder()
    if recorder is not None:
        recorder.set_versions(knowledge_version=str(artifact_id))
    started = time.perf_counter()
    cache_hit = False
    t_rewrite = time.perf_counter()
    question_norm = _normalize_question(question)
    # R-P2-1（F2b-b）改写/HyDE：两独立 flag 默认关 = 基线逐位等价；LLM 失败/
    # 超时/空输出 fail-open 回原 query。实际生效才给缓存池加后缀隔离
    # （沿 hybrid |h1 先例；失败回退原 query ⇒ 与 flag-off 同池，语义相同）。
    effective_norm = question_norm
    cache_suffix = ""
    if settings.RETRIEVAL_QUERY_REWRITE_ENABLED or settings.RETRIEVAL_HYDE_ENABLED:
        try:
            from agentix.retrieval.usecases.query_rewrite import (
                hyde_expand,
                rewrite_query,
            )

            eq = question
            if settings.RETRIEVAL_QUERY_REWRITE_ENABLED:
                eq, rw_applied = await rewrite_query(question=eq, history=history)
                if rw_applied:
                    cache_suffix += "|r1"
            if settings.RETRIEVAL_HYDE_ENABLED:
                hyde_doc, hyde_applied = await hyde_expand(question=eq)
                if hyde_applied:
                    eq = hyde_doc
                    cache_suffix += "|y1"
            eff_norm = _normalize_question(eq)
            if eff_norm:
                effective_norm = eff_norm
            if cache_suffix:
                logger.info(
                    "query intelligence applied suffix=%s kb=%s",
                    cache_suffix,
                    kb_id,
                )
        except Exception:  # noqa: BLE001 - 查询智能故障一律回退原 query
            logger.warning("query intelligence failed (fail-open)", exc_info=True)
            effective_norm = question_norm
            cache_suffix = ""

    # 5W1H 槽位扩展：确定性、零模型调用，只往检索文本追加规范词（原句保留）。
    # 独立于上面两个 LLM flag，默认关 = 基线逐位等价；生效才加缓存池后缀。
    if settings.RETRIEVAL_SLOT_EXPANSION_ENABLED:
        try:
            from agentix.retrieval.usecases.query_slots import expand_query

            slot = expand_query(effective_norm)
            if slot.added_terms:
                effective_norm = slot.search_text
                cache_suffix += "|s1"
        except Exception:  # noqa: BLE001 - 扩展失败保留原检索文本
            logger.warning("query slot expansion failed (fail-open)", exc_info=True)

    rewrite_ms = (time.perf_counter() - t_rewrite) * 1000.0
    hybrid_on = bool(settings.RETRIEVAL_HYBRID_ENABLED)

    # W2：租户解析惰性且只算一次——仅在真正构键（Redis 查询 / SingleFlight
    # 键）时触发；Redis 与 SingleFlight 双关的基线路径零额外查询。
    resolved_tenant: str | None = (
        tenant_id.strip()
        if isinstance(tenant_id, str) and tenant_id.strip()
        else None
    )

    def _tenant_for_key() -> str:
        nonlocal resolved_tenant
        if resolved_tenant is None:
            resolved_tenant = _resolve_cache_tenant(session, kb_id)
        return resolved_tenant

    def _compose_key(*, scope_only: bool = False) -> str:
        key = _retrieval_cache_key(
            kb_id=kb_id,
            artifact_id=artifact_id,
            search_k=search_k,
            # E7：语义层的作用域段就是"去掉问法摘要的同一个键"。走同一个函数是为了
            # 不出现第二个权限/租户隔离源——第二源错了的形态是跨权限命中，不是报错。
            question_norm=None if scope_only else question_norm,
            scope_token=scope_token,
            tenant_id=_tenant_for_key(),
        )
        if hybrid_on:
            # hybrid 结果（融合分/精排序）与 flag-off 结果不共池：
            # 翻开关后不读到异质缓存（TTL 内语义混淆）。
            key += "|h1"
        # R-P2-1：改写/HyDE 实际生效的结果与 flag-off 结果不共池
        key += cache_suffix
        # Dense 候选池精排改变了检索集合与顺序，独立分池
        if settings.RETRIEVAL_DENSE_RERANK_ENABLED:
            key += "|d1"
        # Query 相关性记忆重排改变了检索集合与顺序，独立分池（与 |d1 不共池）
        if settings.RETRIEVAL_QUERY_MEMORY_ENABLED:
            key += "|q1"
        return key

    cache_key = ""
    docs = None
    if cache_redis is not None:
        try:
            cache_key = _compose_key()
            cached = await asyncio.to_thread(cache_redis.get, cache_key)
            if cached:
                docs = _deserialize_docs_from_cache(cached)
                cache_hit = True
        except Exception as e:
            logger.info(f"retrieval cache get failed: {e}")
            docs = None

    # E7 · 召回级语义结果缓存（SEMANTIC_CACHE_ENABLED，默认关 ⇒ 这一段一行都不执行）。
    # 位置刻意在精确键**之后**：精确键命中连 embedding 都不用算，语义层只该处理
    # "换了问法问同一件事"那部分流量；反过来（语义在前）等于给每次命中都白付一次
    # embedding 调用——把省钱件改造成花钱件。
    semantic_peek = None
    if docs is None and settings.SEMANTIC_CACHE_ENABLED:
        from agentix.retrieval.infra.rag.semantic_cache import semantic_recall_peek

        semantic_peek = await asyncio.to_thread(
            semantic_recall_peek, _compose_key(scope_only=True), effective_norm
        )
        if semantic_peek is not None and semantic_peek.match is not None:
            try:
                docs = _deserialize_docs_from_cache(semantic_peek.match.payload.encode("utf-8"))
            except Exception as exc:  # noqa: BLE001 - 坏载荷按未命中处理，照常检索（约定 2）
                logger.warning(f"semantic recall payload rejected; falling back to search: {exc}")
                docs = None
            if not docs:
                # 空载荷不是一次命中：让它走 miss 分支并把**新结果**回写覆盖旧条目，
                # 否则"检索为空"这个状态会一直粘在缓存里，IndexWorker 灌完库也读不到新东西。
                docs = None
    if docs is None:
        # W2：miss 主体收敛为两个闭包——baseline 直跑其一（与旧行为逐位
        # 等价）；adaptive 拆成级联级别；SingleFlight 再包一层在飞合并。
        async def _hybrid_miss() -> list:
            from agentix.retrieval.usecases.retrieval_hybrid import (
                retrieve_hybrid_docs,
                set_stage_report,
            )

            async with span("rag.retrieve", kb_id=kb_id) as _step:
                miss_docs, stage_report = await retrieve_hybrid_docs(
                    kb_id=kb_id,
                    question=question,
                    question_norm=effective_norm,
                    search_k=search_k,
                    metadata_filter=metadata_filter,
                    vectorstore=vectorstore,
                    artifact_id=artifact_id,
                    rewrite_ms=rewrite_ms,
                    session=session,
                )
                _step.set_attribute("docs_count", len(miss_docs or []))
                _step.set_attribute("hybrid", True)
            set_stage_report(stage_report)
            return miss_docs

        async def _dense_miss() -> list:
            async with span("rag.retrieve", kb_id=kb_id) as _step:
                memory_on = bool(settings.RETRIEVAL_QUERY_MEMORY_ENABLED)
                dense_rerank_on = bool(settings.RETRIEVAL_DENSE_RERANK_ENABLED)
                effective_k = search_k
                if memory_on:
                    effective_k = max(
                        int(search_k), int(settings.RETRIEVAL_QUERY_MEMORY_CANDIDATES)
                    )
                if dense_rerank_on and not memory_on:
                    effective_k = max(
                        int(search_k), int(settings.RETRIEVAL_RERANK_CANDIDATES)
                    )
                miss_docs = await asyncio.to_thread(
                    retrieve_docs_with_retry,
                    vectorstore,
                    effective_norm,
                    search_k=effective_k,
                    metadata_filter=metadata_filter,
                )
                if effective_k > search_k and memory_on:
                    # Query 记忆重排自带可选 CE 二次融合；两条重排链互斥，
                    # 避免同一候选池被两次 Cross-Encoder 调用。
                    from agentix.retrieval.usecases.query_memory_rerank import (
                        rerank_docs_with_query_memory,
                    )

                    miss_docs = await rerank_docs_with_query_memory(
                        question, miss_docs, search_k, vectorstore
                    )
                elif effective_k > search_k and dense_rerank_on:
                    from agentix.retrieval.usecases.dense_rerank import (
                        rerank_dense_candidates,
                    )

                    miss_docs = await rerank_dense_candidates(
                        question, miss_docs, search_k
                    )
                _step.set_attribute("docs_count", len(miss_docs or []))
            return miss_docs

        runner = _hybrid_miss if hybrid_on else _dense_miss

        if settings.RETRIEVAL_ADAPTIVE_ENABLED:
            from agentix.retrieval.domain.services.cascade import (
                CascadeLevel,
                classify_query_kind,
                run_cascade,
            )

            kind = classify_query_kind(question)
            dense_level = CascadeLevel(
                "dense_exact",
                _dense_miss,
                higher_is_better=bool(settings.INDEX_SCORE_IS_SIMILARITY),
            )
            levels = [dense_level]
            if hybrid_on:
                hybrid_level = CascadeLevel(
                    "bm25_rrf_rerank", _hybrid_miss, higher_is_better=True
                )
                # multi_hop（graph 未注入的现实档）：融合召回前置，dense 兜底；
                # statute/general：exact 直取前置，低分/空再升级融合精排。
                levels = (
                    [hybrid_level, dense_level]
                    if kind == "multi_hop"
                    else [dense_level, hybrid_level]
                )

            async def _cascade_miss() -> list:
                cdocs, report = await run_cascade(
                    levels,
                    min_score=float(settings.RETRIEVAL_CASCADE_MIN_SCORE),
                )
                logger.info(
                    "adaptive cascade kb=%s kind=%s gate=%s stopped_at=%s n_levels=%d",
                    kb_id,
                    kind,
                    report.get("gate"),
                    report.get("stopped_at"),
                    len(levels),
                )
                return cdocs

            runner = _cascade_miss

        # 检索下游隔离舱（BULKHEAD_RETRIEVAL_SIZE=0 默认直通）。
        # 包住 miss 计算本体：SingleFlight 等待方不占池位，只有 leader 执行。
        _runner_inner = runner

        async def _pooled_runner() -> list:
            from agentix.reliability.bulkhead import bulkhead_slot

            async with bulkhead_slot("retrieval"):
                return await _runner_inner()

        runner = _pooled_runner

        if settings.RETRIEVAL_SINGLEFLIGHT_ENABLED:
            from agentix.infrastructure.cache.singleflight import (
                get_singleflight,
            )

            docs = await get_singleflight().do(
                cache_key or _compose_key(), runner
            )
        else:
            docs = await runner()
        if cache_redis is not None and cache_key and docs:
            try:
                ttl = int(getattr(settings, "RETRIEVAL_CACHE_TTL_SECONDS", 600) or 600)
                ttl = max(30, ttl)
                await asyncio.to_thread(
                    cache_redis.set,
                    cache_key,
                    _serialize_docs_for_cache(docs).encode("utf-8"),
                    ex=ttl,
                    nx=True,
                )
            except Exception as e:
                logger.info(f"retrieval cache set failed: {e}")
        # E7 回写：把这次真检索的结果按**本次 peek 带出来的那个向量**存进语义层。
        # 不重新嵌入一次是有原因的：重新嵌入等于"用来判命中的向量"与"存进去的向量"
        # 分两次取，中间若换了端点/模型，条目就会与自己的判定口径对不上号（约定 6 ④）。
        # peek 为 None（flag 关 / 拿不到向量 / 空输入）⇒ 这里天然什么都不做。
        if semantic_peek is not None and docs:
            from agentix.retrieval.infra.rag.semantic_cache import semantic_remember

            await asyncio.to_thread(
                semantic_remember, semantic_peek, _serialize_docs_for_cache(docs)
            )

    result = docs or []
    # R-P0-5 打点不吞错：埋点失败绝不打挂已成功的检索主链路——
    # 此前 record_retrieval/record_cache_* 抛出会让整个查询 500。
    try:
        from agentix.observability import record_retrieval

        record_retrieval(
            kb_id=kb_id,
            engine=settings.VECTOR_STORE_BACKEND,
            status="hit" if result else "empty",
            duration=time.perf_counter() - started,
            docs_count=len(result),
        )
        if cache_hit:
            from agentix.observability import record_cache_hit

            record_cache_hit("retrieval")
        else:
            # 企业化 P1-5.2：命中率曲线需要 miss 侧数据（此前只记 hit）
            from agentix.observability import record_cache_miss

            record_cache_miss("retrieval")
    except Exception as exc:  # noqa: BLE001 - 埋点失败绝不影响已成功的检索
        logger.warning(f"retrieval telemetry degraded (R-P0-5): {exc}")
        try:
            from agentix.observability import record_degraded

            record_degraded("chat_retrieval")
        except Exception:  # noqa: BLE001 - 计数上报失败不再二次抛出
            logger.debug("record_degraded counter also failed (non-fatal)")
    return result


def get_citations_from_docs(docs, session: Session) -> list[dict]:
    """从检索结果构建引用信息（含文件级元信息，供前端悬浮卡片展示）。

    按 document 聚合：一次 IN 查全部 chunk、三次 IN 查全部文件级元信息，
    不再逐条 ``get_chunk_by_id`` + ``get_document_file_meta``（原本 2N 次查询）。
    """
    from agentix.retrieval.infra.repos.document_repo import (
        _empty_file_meta,
        get_chunks_by_ids,
        get_document_file_metas,
    )

    metas = [dict(d.metadata or {}) for d in docs]
    chunk_ids = [m.get("chunk_id") for m in metas if m.get("chunk_id")]
    doc_ids = [m.get("document_id") for m in metas if m.get("document_id")]
    chunks = get_chunks_by_ids(session, chunk_ids)
    file_metas = get_document_file_metas(session, doc_ids)

    from agentix.domain.policies.legal_effectiveness import effective_status_of

    citations: list[dict] = []
    for doc, meta in zip(docs, metas):
        chunk_id = meta.get("chunk_id")
        document_id = meta.get("document_id")
        chunk = chunks.get(str(chunk_id)) if chunk_id else None
        file_meta = file_metas.get(str(document_id), _empty_file_meta())

        source_name = meta.get("source", "")
        if document_id and not source_name:
            source_name = file_meta["document_title"] or source_name

        snippet = doc.page_content[:200] + ("..." if len(doc.page_content) > 200 else "")

        # R2-2（指南 §2.2.7①）：sources 带 status（缺失 → in_force，供引用卡展示有效性状态）
        replaced = meta.get("replaced_by")
        citations.append(
            {
                "chunk_id": chunk_id,
                "document_id": document_id,
                "source": source_name,
                "page_no": chunk.page_no if chunk else meta.get("page"),
                "snippet": snippet,
                "effective_status": effective_status_of(meta or {}),
                # 失效替代提示（前端 CitationPreviewCard 消费；无则 None）
                "replaced_by": str(replaced) if replaced else None,
                **file_meta,
            }
        )
    return citations


# hydrate_citations_from_stored 已迁至 agentix.retrieval.usecases.citation_hydrate
# （chat_service 与 conversation_service 共用的共享 helper，避免 usecases 互相 import）。


def build_prompt(
    history_text: str,
    context: str,
    question: str,
    *,
    target_document: str | None = None,
    status_fields: dict | None = None,
) -> str:
    # 企业化 P1-4.2 ①：文档数据定界包裹 + system 侧声明（数据 ≠ 指令）
    from agentix.config.settings import settings as main_settings
    from agentix.security.prompt_injection import build_boundary_notice, wrap_document_content

    boundary_notice = (
        build_boundary_notice() if main_settings.PROMPT_INJECTION_DETECTION_ENABLED else ""
    )
    wrapped_context = (
        wrap_document_content(context) if main_settings.PROMPT_INJECTION_DETECTION_ENABLED else context
    )
    prompt = f"""
    你是 My Agentix，一个基于文档的助手。请严格基于下方文档内容回答问题。
    {boundary_notice}
    """
    if target_document:
        prompt += f"""
本次检索已限定在文档「{target_document}」内，下方片段均来自该文档。
请据此作答；只要片段中有可用信息就必须回答，禁止称该文档不存在或不在范围内。
"""
    if history_text:
        prompt += """你可以参考历史对话来理解用户意图，但回答必须基于文档内容。

历史对话：
{history_text}

"""
    prompt += """
相关文档：
{context}

用户问题：
{question}
"""
    # R3-5（指南 L1⑦）：状态栏字段——scope/召回数/案件阶段等单行注入；默认不传则行为不变
    if status_fields:
        from agentix.retrieval.domain.services.context_engine import build_status_bar

        status_bar = build_status_bar(**status_fields)
        if status_bar:
            prompt += "\n" + status_bar + "\n"
    return prompt.format(
        history_text=history_text, context=wrapped_context, question=question
    )


def _usage_error_code_from_event(payload: dict) -> str | None:
    """从 SSE 事件推断运营记录用的 error_code（成功路径返回 None）。"""
    ptype = payload.get("type")
    if ptype == "error":
        return str(payload.get("code") or ErrorCode.INTERNAL_ERROR)
    if ptype == "answer_chunk" and payload.get("finished"):
        content = (payload.get("content") or "").strip()
        if content == INSUFFICIENT_RETRIEVAL_MSG:
            return "INSUFFICIENT_RETRIEVAL"
        if content == "索引未就绪":
            return ErrorCode.INDEX_NOT_READY
    return None


async def _stream_answer_impl(
    session: Session,
    *,
    kb_id: str,
    question: str,
    conversation_id: str | None,
    request_id: str,
    user_id: str,
    cache_redis: redis.Redis | None,
    document_id: str | None = None,
    folder_id: str | None = None,
    chat_mode: ChatMode = "fast",
) -> AsyncGenerator[dict, None]:
    """包装流式问答：R5a 入控制面 + 帧戳记（seq/run_id）；结束时写 UsageRecord。"""
    cfg = get_default_config()
    model_name = resolve_chat_model(cfg.get("model"))
    t0 = time.perf_counter()
    usage_error: str | None = None
    tokens_in = 0
    tokens_out = 0

    # Trajectory：请求级步骤轨迹（仅失败落盘，除非 TRAJECTORY_DUMP_ALL）
    recorder = None
    trace_token = None
    rec_token = None
    if settings.TRAJECTORY_ENABLED:
        from agentix.observability.trajectory import TrajectoryRecorder, set_recorder
        from agentix.observability.context import bind_trace_id

        from agentix.observability.versions import PROMPT_VERSION
        recorder = TrajectoryRecorder(
            "chat.answer",
            kb_id=kb_id,
            question=question,
            prompt_version=PROMPT_VERSION,
            model_version=model_name,
            # The active index version is resolved by the retrieval step and
            # can be replaced with the exact artifact id by callers.
            knowledge_version=f"kb-{kb_id}",
        )
        trace_token = bind_trace_id(recorder.trace_id)
        rec_token = set_recorder(recorder)

    # --- A5/R5a：流式路径入控制面（与编排 chat _stream_events_impl 同款登记语义）---
    # spawn → bind（当前 SSE 消费任务）→ running → 终态收口。登记在**首帧
    # 之前**：准入拒绝（AdmissionDenied）fail-closed 上抛；其他登记故障降级
    # 为不追踪（帧仍带 seq、无 run_id），不影响事件流。
    from agentix.application.services.run_event_stream import RunEventEmitter
    from agentix.runtime.control_plane import (
    AdmissionDenied,
    get_control_plane,
)
    from agentix.domain.tracing import get_trace_id

    plane = None
    run_id: str | None = None
    emitter = RunEventEmitter()  # 降级默认：有 seq 无 run_id
    finished_seen = False

    def _finish_run(state: str | None = None, error: str | None = None) -> None:
        """R5a 流式运行终态收口（幂等；故障不影响事件流）。"""
        if plane is None or run_id is None:
            return
        try:
            from agentix.runtime.control_plane import (
    RunState as _RunState,
)

            if state is None:
                plane.finish(run_id)
            else:
                plane.finish(run_id, state=_RunState(state), error=error)
        except Exception as exc:  # noqa: BLE001
            logger.debug("retrieval stream run finish skipped: %s", exc)

    try:
        # 准入三态（flag 关直通）；拒绝经既有 except
        # AdmissionDenied fail-closed 上抛（HTTP 层映射 429）。
        from agentix.performance.admission import admission_wait

        await admission_wait(kind="retrieval_stream")
        plane = get_control_plane()
        run = plane.spawn(
            "retrieval_chat",
            trace_id=get_trace_id(),
            meta={
                "channel": "stream",
                "domain": "retrieval",
                "conversation_id": conversation_id or "",
            },
        )
        run_id = run.run_id
        current = asyncio.current_task()
        if current is not None:
            plane.bind(run_id, current)
        plane.mark_running(run_id)
        emitter = RunEventEmitter(run_id)
    except AdmissionDenied as exc:
        logger.warning("control plane admission denied (retrieval stream): %s", exc)
        raise
    except Exception as exc:  # noqa: BLE001 - 登记失败不得阻断事件流
        logger.debug("control plane spawn skipped (retrieval stream): %s", exc)
        plane, run_id = None, None

    try:
        async for payload in _stream_answer_events(
            session,
            kb_id=kb_id,
            question=question,
            conversation_id=conversation_id,
            request_id=request_id,
            user_id=user_id,
            cache_redis=cache_redis,
            document_id=document_id,
            folder_id=folder_id,
            chat_mode=chat_mode,
            model_name=model_name,
        ):
            if recorder is not None and payload.get("type") == "meta":
                recorder.conversation_id = str(payload.get("conversation_id") or "")
            ec = _usage_error_code_from_event(payload)
            if ec:
                usage_error = ec
            tokens_in = max(tokens_in, int(payload.get("tokens_in", 0) or 0))
            tokens_out = max(tokens_out, int(payload.get("tokens_out", 0) or 0))
            if payload.get("finished"):
                finished_seen = True
            # A5/R5a：单点给每帧打 seq/run_id（守卫 #5 载体）
            yield emitter.attach(payload)
    except asyncio.CancelledError:
        # 服务端关停等取消：先收口终态再传播（finish 幂等不覆盖）
        _finish_run("cancelled", "cancelled")
        raise
    except GeneratorExit:
        # 客户端断连 / api_chat 聚合到 finished 后提前关闭：终态帧已交付
        # 的按（失败帧→FAILED，否则→SUCCEEDED）由 finally 收口；未交付
        # 则按断连收口（GeneratorExit 期间禁止 yield）
        if not finished_seen:
            _finish_run("cancelled", "client_disconnected")
        raise
    except Exception as e:
        # A5：吞异常并产出已戳记的结构化 error 帧——SSE 契约要求明确
        # 终态帧，api_chat 聚合器同样消费 error 帧（→ AppError）
        logger.exception("retrieval stream failed: %s", e)
        _finish_run("failed", f"{type(e).__name__}: {e}")
        yield emitter.attach(
            {
                "type": "error",
                "code": ErrorCode.INTERNAL_ERROR,
                "content": "处理请求时出错，请稍后重试。",
            }
        )
        return
    finally:
        # A5/R5a 终态收口：拒答/错误帧 → FAILED；其余（含已交付 finished 后
        # 被提前关闭）→ SUCCEEDED；先到者已生效（finish 幂等不覆盖）
        if usage_error is not None:
            _finish_run("failed", str(usage_error))
        else:
            _finish_run()
        if recorder is not None:
            failed = usage_error is not None or sys.exc_info()[0] is not None
            recorder.end("error" if failed else "ok")
            # 预算/轮次终止也落盘，供 replay_trace.py 回放复核（M2.4）
            if failed or settings.TRAJECTORY_DUMP_ALL or recorder.terminate_reason is not None:
                recorder.dump()
            from agentix.observability.context import reset_trace_id
            from agentix.observability.trajectory import reset_recorder

            reset_trace_id(trace_token)
            reset_recorder(rec_token)

        from agentix.retrieval.infra.repos.usage_repo import record_chat_stream_usage

        latency_ms = int((time.perf_counter() - t0) * 1000)
        record_chat_stream_usage(
            session,
            request_id=request_id,
            kb_id=kb_id,
            user_id=user_id,
            chat_mode=chat_mode,
            model_name=model_name,
            latency_ms=latency_ms,
            error_code=usage_error,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        from agentix.observability import record_llm_usage

        record_llm_usage(
            model=model_name,
            provider="chat",
            prompt_tokens=tokens_in,
            completion_tokens=tokens_out,
            duration=latency_ms / 1000,
        )


def _prepare_conversation_and_scope(
    session: Session,
    *,
    kb_id: str,
    question: str,
    conversation_id: str | None,
    user_id: str,
    document_id: str | None = None,
    folder_id: str | None = None,
) -> tuple[str, Message, str, str | None] | dict:
    """
    开流前准备：KB 归属 → 会话/范围锁定 → 落用户消息。

    Returns:
        成功：(cid, user_msg, scope_type, scope_id)
        失败：SSE error payload（由调用方 yield 后 return）
    """
    try:
        require_kb_owned(session, kb_id, user_id)
    except AppError as e:
        return {"type": "error", "code": e.error_code, "content": e.message}

    from agentix.retrieval.usecases.chat_scope import (
        resolve_effective_scope,
        scope_from_request,
        validate_scope,
    )

    # 复用已有会话；不存在则返回错误（M3.6: 不静默创建新会话）。无权则直接 error。
    cid = conversation_id
    conv: Conversation | None = None
    if cid:
        conv = get_conversation_by_id(session, cid)
        if not conv:
            return {
                "type": "error",
                "code": ErrorCode.CONVERSATION_NOT_FOUND,
                "content": ERROR_CODE_TO_HINT.get(
                    ErrorCode.CONVERSATION_NOT_FOUND, "会话不存在"
                ),
            }
        elif conv.user_id != user_id or conv.kb_id != kb_id:
            return {
                "type": "error",
                "code": ErrorCode.FORBIDDEN,
                "content": "无权访问该会话",
            }

    # 已有会话沿用其 scope；新会话用本次请求的 document_id/folder_id
    scope_type, scope_id = resolve_effective_scope(
        conversation=conv,
        document_id=document_id,
        folder_id=folder_id,
    )

    # M3.3: 上下文隔离防御性检查——续聊时请求参数与会话锁定 scope 不一致则拒绝
    if conv is not None and (document_id or folder_id):
        req_scope_type, req_scope_id = scope_from_request(
            document_id=document_id, folder_id=folder_id
        )
        if req_scope_type != scope_type or req_scope_id != scope_id:
            logger.error(
                "context isolation violation: conversation %s locked to scope (%s, %s) "
                "but request provides (%s, %s)",
                cid, scope_type, scope_id, req_scope_type, req_scope_id,
            )
            return {
                "type": "error",
                "code": ErrorCode.CONTEXT_ISOLATION_VIOLATION,
                "content": ERROR_CODE_TO_HINT.get(
                    ErrorCode.CONTEXT_ISOLATION_VIOLATION,
                    "请求范围与会话锁定范围不一致",
                ),
            }

    try:
        validate_scope(session, kb_id=kb_id, scope_type=scope_type, scope_id=scope_id)
    except AppError as e:
        return {"type": "error", "code": e.error_code, "content": e.message}

    if not cid:
        # 新会话锁定 scope（kb / folder / file），后续多轮不再变范围
        new_scope_type, new_scope_id = scope_from_request(
            document_id=document_id, folder_id=folder_id
        )
        conv = create_conversation(
            session,
            kb_id,
            user_id,
            question[:30],
            scope_type=new_scope_type,
            scope_id=new_scope_id,
        )
        cid = conv.id
        scope_type, scope_id = new_scope_type, new_scope_id

    # 先落用户消息，后续检索/生成失败也能在历史里看到提问
    user_msg = Message(conversation_id=cid, role="user", content=question)
    session.add(user_msg)
    session.commit()
    session.refresh(user_msg)
    return cid, user_msg, scope_type, scope_id


def _ensure_kb_ready_or_reject(
    session: Session,
    *,
    kb_id: str,
    cid: str,
    user_msg: Message,
    scope_type: str,
    scope_id: str | None,
    model_name: str,
    request_id: str,
):
    """
    开答前就绪检查：库/索引/scope 有内容，并加载向量库。

    失败时返回 finished answer_chunk（已写入助手拒答，便于历史回放）；
    成功返回 (vectorstore, metadata_filter, scope_token, artifact_id)。
    """
    from agentix.retrieval.infra.repos.document_repo import has_any_document, has_any_chunk
    from agentix.retrieval.usecases.chat_scope import (
        SCOPE_FOLDER,
        build_faiss_filter,
        document_ids_for_scope,
        scope_cache_token,
    )

    kb_empty_hint = ERROR_CODE_TO_HINT.get(ErrorCode.KB_EMPTY, "知识库为空,请先上传文档")
    if not has_any_document(session, kb_id):
        return _finished_answer_chunk(
            session,
            conversation_id=cid,
            question_message_id=user_msg.id,
            answer_text=kb_empty_hint,
            model_name=model_name,
            request_id=request_id,
        )

    if not has_any_chunk(session, kb_id):
        return _finished_answer_chunk(
            session,
            conversation_id=cid,
            question_message_id=user_msg.id,
            answer_text=kb_empty_hint,
            model_name=model_name,
            request_id=request_id,
        )

    artifact = get_active_index_artifact(session, kb_id)
    if not artifact:
        return _finished_answer_chunk(
            session,
            conversation_id=cid,
            question_message_id=user_msg.id,
            answer_text=ERROR_CODE_TO_HINT.get(
                ErrorCode.INDEX_NOT_READY, "文档正在处理中，请稍后再试"
            ),
            model_name=model_name,
            request_id=request_id,
        )

    # 空文件夹等：范围内无可检索文档，不必再装向量库
    doc_ids = document_ids_for_scope(
        session, kb_id=kb_id, scope_type=scope_type, scope_id=scope_id
    )
    if doc_ids is not None and len(doc_ids) == 0:
        empty_msg = (
            "该文件夹下暂无可检索文档"
            if scope_type == SCOPE_FOLDER
            else "当前范围内暂无可检索内容"
        )
        return _finished_answer_chunk(
            session,
            conversation_id=cid,
            question_message_id=user_msg.id,
            answer_text=empty_msg,
            model_name=model_name,
            request_id=request_id,
        )

    # 预检与 Agent 工具共用同一套 filter / cache token
    metadata_filter = build_faiss_filter(doc_ids)
    # 对检索不可见的文档：存在 pending 删除审批的 + 已标记删除待物理清理的
    # （后者在重建任务清 chunk 之前，旧 active 索引里仍有其向量，必须在此立即排除）
    from agentix.retrieval.usecases.chat_scope import merge_exclusion_filter, non_retrievable_document_ids

    metadata_filter = merge_exclusion_filter(
        metadata_filter, non_retrievable_document_ids(session, kb_id)
    )
    scope_token = scope_cache_token(scope_type, scope_id)
    artifact_id = getattr(artifact, "id", "") or str(getattr(artifact, "version", ""))

    try:
        # R-P3：默认关 ⇒ 等价于 build_retriever；开启则复用进程内已构造实例
        # （省掉每次预检检索的客户端重建 + FAISS 反序列化，docs/已知限制.md 登记口径）。
        from agentix.retrieval.infra.vectorstore.retriever_cache import (
            get_or_build_retriever,
        )

        _, vectorstore = get_or_build_retriever(artifact.index_path)
    except FileNotFoundError:
        return _finished_answer_chunk(
            session,
            conversation_id=cid,
            question_message_id=user_msg.id,
            answer_text=ERROR_CODE_TO_HINT.get(
                ErrorCode.INDEX_FILE_NOT_FOUND, "索引文件不存在"
            ),
            model_name=model_name,
            request_id=request_id,
        )

    return vectorstore, metadata_filter, scope_token, artifact_id


async def _apply_output_filter(answer_text: str, *, tool_ctx, question: str) -> str:
    """答案落库前的输出过滤（T17 薄封装，避免 _stream_agent_and_persist 膨胀）。"""
    from agentix.retrieval.usecases.chat_output_filter import filter_answer

    filtered, _info = await filter_answer(
        answer_text, contexts=tool_ctx.last_contexts, question=question
    )
    return filtered


def _setup_agent_context(
    session: Session,
    *,
    kb_id: str,
    scope_type: str,
    scope_id: str | None,
    vectorstore,
    metadata_filter,
    search_k: int,
    cache_redis: redis.Redis | None,
    artifact_id: str,
    scope_token: str,
    model_name: str,
    chat_mode: ChatMode,
):
    """创建 AgentToolContext / LLM / scope_label，供 _stream_agent_and_persist 编排。"""
    from agentix.retrieval.usecases.chat_agent_tools import AgentToolContext
    from agentix.retrieval.usecases.chat_scope import scope_prompt_label

    tool_ctx = AgentToolContext(
        session=session,
        kb_id=kb_id,
        scope_type=scope_type,
        scope_id=scope_id,
        vectorstore=vectorstore,
        metadata_filter=metadata_filter,
        search_k=search_k,
        cache_redis=cache_redis,
        artifact_id=artifact_id,
        scope_token=scope_token,
        model_name=model_name,
        chat_mode=chat_mode,
    )
    llm = create_chat_llm(model_name=model_name, chat_mode=chat_mode)
    scope_label = scope_prompt_label(
        session, kb_id=kb_id, scope_type=scope_type, scope_id=scope_id
    )
    return tool_ctx, llm, scope_label


async def _post_process_and_persist(
    session: Session,
    *,
    kb_id: str,
    cid: str,
    user_msg: Message,
    question: str,
    search_k: int,
    model_name: str,
    answer_text: str,
    tool_ctx,
) -> tuple[Answer, Message, list]:
    """输出过滤 → 引用校验 → 审计落库 → Answer/Message 持久化，返回 (answer, assistant_msg, citations)。"""
    from agentix.retrieval.domain.services.citation_verify import verify_citations_traceable

    answer_text = await _apply_output_filter(answer_text, tool_ctx=tool_ctx, question=question)
    citations = tool_ctx.last_citations

    citations, stripped = verify_citations_traceable(citations, tool_ctx.last_hit_chunks)
    if stripped:
        logger.warning("stripped %d untraceable citations", len(stripped))

    # R1-2 引用验证器 v1（指南 §2.1.5⑥）：答案文本数据条目引用 ↔ 检索证据交叉核验，
    # 未核验通过的引用显式标注在答案尾部（核验失败绝不抛错、绝不阻断落库）。
    unverified_refs: list = []
    try:
        from agentix.retrieval.domain.services.citation_verify import verify_answer_citations

        answer_text, _verified_refs, unverified_refs = verify_answer_citations(
            answer_text, tool_ctx.last_hit_chunks
        )
        if unverified_refs:
            logger.warning(
                "answer contains %d unverified statute citations", len(unverified_refs)
            )
    except Exception:  # noqa: BLE001 - 验证器故障不得影响答案交付
        logger.warning("citation verifier failed (non-fatal)", exc_info=True)

    # R2-2 数据条目有效性过滤（指南 §2.2.7①）：有检索命中时答案尾部标注"检索截至日期"
    if tool_ctx.last_hit_chunks and settings.EFFECTIVENESS_ASOF_ANNOTATION:
        try:
            from agentix.domain.policies.legal_effectiveness import append_retrieved_asof

            answer_text = append_retrieved_asof(answer_text)
        except Exception:  # noqa: BLE001 - 标注失败不影响答案交付
            logger.warning("retrieved-as-of annotation failed (non-fatal)", exc_info=True)

    # R3-6 GroundingValidator（架构文档 §104）：句子级证据锚定校验——观测模式
    # 只产出报告（tool_ctx.last_grounding）+ 低分 WARN，不改答案、不拦截交付。
    if settings.GROUNDING_VALIDATOR_ENABLED:
        try:
            from agentix.domain.policies.grounding_verify import validate_answer_grounding

            report = validate_answer_grounding(
                answer_text,
                tool_ctx.last_hit_chunks,
                support_threshold=settings.GROUNDING_SUPPORT_THRESHOLD,
            )
            tool_ctx.last_grounding = report.to_dict()
            if (
                report.skipped_reason is None
                and report.score is not None
                and settings.GROUNDING_LOW_SCORE_WARN > 0
                and report.score < settings.GROUNDING_LOW_SCORE_WARN
            ):
                logger.warning(
                    "grounding score low: %.2f (%d/%d claims anchored), unsupported: %s",
                    report.score,
                    report.supported_claims,
                    report.total_claims,
                    report.unsupported,
                    extra={"stage": "grounding_validator", "status": "low_score"},
                )
                # P2 可观测（O1）：低分率进 Prometheus（观测口径，低分率突增可告警）
                try:
                    from agentix.observability.metrics import record_grounding_low_score

                    record_grounding_low_score()
                except Exception:  # noqa: BLE001 - 指标故障不影响主链路
                    pass
        except Exception:  # noqa: BLE001 - 校验器故障不得影响答案交付
            logger.warning("grounding validator failed (non-fatal)", exc_info=True)

    # R-P2-2 证据门禁（rag-optimization §6）：citation/grounding 两验证器结果 →
    # 可配门禁。observe（默认）= 与基线逐位等价；warn 附一条警告；refuse 降级
    # 拒答（答案替换 + citations 清空）。门禁自身故障 fail-open 回基线交付。
    try:
        from agentix.domain.policies.evidence_gate import (
            ACTION_REFUSE,
            EVIDENCE_GATE_REFUSAL_MSG,
            build_warn_footer,
            decide_evidence_gate,
        )

        _g = tool_ctx.last_grounding or {}
        _decision = decide_evidence_gate(
            mode=settings.EVIDENCE_GATE_MODE,
            grounding_score=_g.get("score"),
            grounding_skipped_reason=_g.get("skipped_reason"),
            warn_threshold=settings.GROUNDING_LOW_SCORE_WARN,
            unverified_citations=len(unverified_refs),
            stripped_citations=len(stripped),
        )
        if _decision.action == ACTION_REFUSE:
            answer_text = EVIDENCE_GATE_REFUSAL_MSG
            citations = []
        elif _decision.triggers and _decision.action == "warn":
            answer_text = (
                answer_text
                + "\n\n"
                + build_warn_footer(
                    _decision,
                    score=_g.get("score"),
                    unverified=len(unverified_refs),
                    stripped=len(stripped),
                )
            )
        if _decision.action != "deliver":
            logger.warning(
                "evidence gate action=%s mode=%s triggers=%s",
                _decision.action,
                _decision.mode,
                list(_decision.triggers),
                extra={"stage": "evidence_gate", "action": _decision.action},
            )
            try:
                from agentix.observability.metrics import record_evidence_gate

                record_evidence_gate(_decision.action)
            except Exception:  # noqa: BLE001 - 指标故障不影响主链路
                pass
        tool_ctx.last_evidence_gate = _decision.to_dict()
    except Exception:  # noqa: BLE001 - 门禁故障 fail-open 回基线交付
        logger.warning("evidence gate failed (non-fatal)", exc_info=True)

    if tool_ctx.last_hit_chunks:
        retrieval_run = RetrievalRun(
            kb_id=kb_id,
            conversation_id=cid,
            question_message_id=user_msg.id,
            query_text=question,
            top_k=search_k,
            hit_chunks=tool_ctx.last_hit_chunks,
        )
        session.add(retrieval_run)

    answer = Answer(
        conversation_id=cid,
        question_message_id=user_msg.id,
        version=1,
        is_current=True,
        answer_text=answer_text,
        citations=[
            {
                "chunk_id": c.get("chunk_id"),
                "document_id": c.get("document_id"),
                "page_no": c.get("page_no"),
                "snippet": c.get("snippet"),
                "source": c.get("source"),
                "document_title": c.get("document_title"),
                "effective_status": c.get("effective_status"),
                "replaced_by": c.get("replaced_by"),
                "file_name": c.get("file_name"),
                "folder_path": c.get("folder_path"),
                "file_size": c.get("file_size"),
                "file_ext": c.get("file_ext"),
            }
            for c in citations
        ],
        model_name=model_name,
    )
    session.add(answer)
    session.commit()
    session.refresh(answer)

    assistant_msg = Message(
        conversation_id=cid,
        role="assistant",
        content=answer_text,
        answer_id=answer.id,
    )
    session.add(assistant_msg)
    session.commit()
    session.refresh(assistant_msg)

    return answer, assistant_msg, citations


async def _maybe_compress_safe(session: Session, *, cid: str, model_name: str) -> None:
    """长会话摘要压缩（回答后执行，不阻断流式回答；失败降级保留原文）。"""
    try:
        from agentix.retrieval.usecases.conversation_compression import maybe_compress_conversation

        await maybe_compress_conversation(session, conversation_id=cid, model_name=model_name)
    except Exception as e:
        logger.error("conversation compression skipped: %s", e)


def _build_finished_payload(
    *,
    cid: str,
    request_id: str,
    assistant_msg: Message,
    answer: Answer,
    citations: list,
    terminate_reason: str | None,
) -> dict:
    """构建 finished 事件（空 content + citations / message_id，前端据此收束流）。"""
    payload: dict = {
        "type": "answer_chunk",
        "content": "",
        "request_id": request_id,
        "finished": True,
        "conversation_id": cid,
        "message_id": assistant_msg.id,
        "answer_id": answer.id,
        "citations": citations,
    }
    if terminate_reason is not None:
        payload["terminate_reason"] = terminate_reason
    return payload


async def _stream_agent_and_persist(
    session: Session,
    *,
    kb_id: str,
    cid: str,
    user_msg: Message,
    question: str,
    history_text: str,
    scope_type: str,
    scope_id: str | None,
    vectorstore,
    metadata_filter,
    scope_token: str,
    artifact_id: str,
    search_k: int,
    cache_redis: redis.Redis | None,
    chat_mode: ChatMode,
    model_name: str,
    request_id: str,
) -> AsyncGenerator[dict, None]:
    """
    正式出答闭环：Agent 流式生成 → 检索审计 → 落库 → finished 事件。
    """
    from agentix.retrieval.usecases.chat_agent_runner import run_agent_stream

    tool_ctx, llm, scope_label = _setup_agent_context(
        session,
        kb_id=kb_id,
        scope_type=scope_type,
        scope_id=scope_id,
        vectorstore=vectorstore,
        metadata_filter=metadata_filter,
        search_k=search_k,
        cache_redis=cache_redis,
        artifact_id=artifact_id,
        scope_token=scope_token,
        model_name=model_name,
        chat_mode=chat_mode,
    )

    # 流式吐字：deep 暴露 tool_running；答案片段累积后一次性落库
    full: list[str] = []
    terminate_reason: str | None = None
    try:
        async for event in run_agent_stream(
            llm=llm,
            ctx=tool_ctx,
            question=question,
            history_text=history_text,
            chat_mode=chat_mode,
            scope_label=scope_label,
        ):
            kind = event.get("kind")
            content = event.get("content") or ""
            if kind == "terminated":
                terminate_reason = event.get("terminate_reason")
                # M2.4: 记录终止类型到 trajectory（幂等——terminate_reason 仅首次写入生效）
                if terminate_reason:
                    from agentix.observability.trajectory import current_recorder

                    rec = current_recorder()
                    if rec is not None and rec.terminate_reason is None:
                        rec.terminate_reason = terminate_reason
                if content:
                    full.append(content)
                    yield {
                        "type": "answer_chunk",
                        "content": content,
                        "request_id": request_id,
                        "finished": False,
                    }
                continue
            if not content:
                continue
            if kind == "tool_running":
                if chat_mode == "deep":
                    yield {
                        "type": "tool_running",
                        "content": content,
                        "request_id": request_id,
                        "finished": False,
                    }
                continue
            if kind == "answer_delta":
                full.append(content)
                yield {
                    "type": "answer_chunk",
                    "content": content,
                    "request_id": request_id,
                    "finished": False,
                }
    except Exception as e:
        logger.exception(f"agent stream failed: {e}")
        yield _finished_answer_chunk(
            session,
            conversation_id=cid,
            question_message_id=user_msg.id,
            answer_text=ERROR_CODE_TO_HINT.get(
                ErrorCode.RETRIEVAL_FAILED, "对话服务暂时不可用"
            ),
            model_name=model_name,
            request_id=request_id,
        )
        return

    answer_text = "".join(full)
    answer, assistant_msg, citations = await _post_process_and_persist(
        session,
        kb_id=kb_id,
        cid=cid,
        user_msg=user_msg,
        question=question,
        search_k=search_k,
        model_name=model_name,
        answer_text=answer_text,
        tool_ctx=tool_ctx,
    )

    await _maybe_compress_safe(session, cid=cid, model_name=model_name)

    yield _build_finished_payload(
        cid=cid,
        request_id=request_id,
        assistant_msg=assistant_msg,
        answer=answer,
        citations=citations,
        terminate_reason=terminate_reason,
    )


async def _stream_answer_events(
    session: Session,
    *,
    kb_id: str,
    question: str,
    conversation_id: str | None,
    request_id: str,
    user_id: str,
    cache_redis: redis.Redis | None,
    document_id: str | None = None,
    folder_id: str | None = None,
    chat_mode: ChatMode = "fast",
    model_name: str,
) -> AsyncGenerator[dict, None]:
    """chat.answer 主 span：包住整条流式问答链路（R3.6 接入点 1）。"""
    async with span("chat.answer", kb_id=kb_id):
        async for payload in _stream_answer_events_impl(
            session,
            kb_id=kb_id,
            question=question,
            conversation_id=conversation_id,
            request_id=request_id,
            user_id=user_id,
            cache_redis=cache_redis,
            document_id=document_id,
            folder_id=folder_id,
            chat_mode=chat_mode,
            model_name=model_name,
        ):
            yield payload


async def _stream_answer_events_impl(
    session: Session,
    *,
    kb_id: str,
    question: str,
    conversation_id: str | None,
    request_id: str,
    user_id: str,
    cache_redis: redis.Redis | None,
    document_id: str | None = None,
    folder_id: str | None = None,
    chat_mode: ChatMode = "fast",
    model_name: str,
) -> AsyncGenerator[dict, None]:
    """
    SSE 流式问答编排（由 _stream_answer_impl / ChatService 转发）。

    主链路：准备会话 → meta → 就绪检查 →（可选）思考前奏 → 检索预检 → Agent 出答落库。
    各阶段通过 yield 推送 SSE；失败可提前 return。
    """
    from agentix.retrieval.usecases.chat_service_helpers import (
        _prepare_context,
        _stream_thinking_prelude_if_deep,
        _stream_retrieval_preflight,
        _stream_final_answer,
    )
    
    search_k = int(get_default_config().get("search_k", settings.KB_TOP_K) or settings.KB_TOP_K)

    # --- 1. 准备：归属 / 会话 / scope / 用户消息 ---
    prepared = _prepare_conversation_and_scope(
        session,
        kb_id=kb_id,
        question=question,
        conversation_id=conversation_id,
        user_id=user_id,
        document_id=document_id,
        folder_id=folder_id,
    )
    if isinstance(prepared, dict):
        yield prepared
        return
    cid, user_msg, scope_type, scope_id = prepared

    # --- 2. meta：尽早把 conversation_id 交给前端 ---
    yield {
        "type": "meta",
        "request_id": request_id,
        "conversation_id": cid,
        "question_message_id": user_msg.id,
    }

    # --- 2.5 进度事件（SSE progress；前端 chatStreamHandlers 消费）---
    # 后端此前从不产出 progress，前端处理分支是死代码（审查 2026-09-27 接线）。
    # 只在编排层**真实边界**打点：loading（准备）→ retrieval（检索预检）→
    # llm_generate（出答）；hybrid 开启时检索内部产出的四阶段（R-P1-5）在预检
    # 后追加一帧 breakdown（flag-off 三段帧逐字节不变）。
    yield {
        "type": "progress",
        "stage": "loading",
        "message": "正在准备会话",
        "detail": {"step": 1, "total": 3},
    }

    # --- 3. 就绪：空库 / 空 scope / 装向量库；失败则拒答并结束 ---
    ready = _ensure_kb_ready_or_reject(
        session,
        kb_id=kb_id,
        cid=cid,
        user_msg=user_msg,
        scope_type=scope_type,
        scope_id=scope_id,
        model_name=model_name,
        request_id=request_id,
    )
    if isinstance(ready, dict):
        yield ready
        return
    vectorstore, metadata_filter, scope_token, artifact_id = ready

    # --- 4. 准备上下文 ---
    context = await _prepare_context(
        session,
        cid=cid,
        kb_id=kb_id,
        scope_type=scope_type,
        scope_id=scope_id,
        chat_mode=chat_mode,
        question=question,
    )
    history_text = context["history_text"]
    scope_label = context["scope_label"]

    # --- 5. 思考前奏：仅 deep；fast 跳过 ---
    scope_doc_count = count_documents_in_scope(
        session, kb_id=kb_id, scope_type=scope_type, scope_id=scope_id
    )
    async for think_delta in _stream_thinking_prelude_if_deep(
        chat_mode=chat_mode,
        model_name=model_name,
        question=question,
        history_text=history_text,
        scope_label=scope_label,
        scope_doc_count=scope_doc_count,
        request_id=request_id,
    ):
        yield think_delta

    # --- 6. 检索预检：内容题无命中则拒答；元问题（统计/列表）跳过 ---
    # R-P1-5：hybrid 四阶段报告由检索在预检内部产出（ContextVar 旁路，生成器
    # 无法逐步 yield）——预检前清空、预检后取走；flag-off 恒为 None ⇒ 三段帧不变。
    from agentix.retrieval.usecases.retrieval_hybrid import (
        pop_stage_report,
        set_stage_report,
    )

    set_stage_report(None)
    yield {
        "type": "progress",
        "stage": "retrieval",
        "message": "正在检索知识库",
        "detail": {"step": 2, "total": 3},
    }
    preflight_result = await _stream_retrieval_preflight(
        session,
        kb_id=kb_id,
        question=question,
        search_k=search_k,
        scope_type=scope_type,
        scope_id=scope_id,
        metadata_filter=metadata_filter,
        vectorstore=vectorstore,
        cache_redis=cache_redis,
        artifact_id=artifact_id,
        scope_token=scope_token,
        cid=cid,
        user_msg=user_msg,
        model_name=model_name,
        request_id=request_id,
        history_text=history_text,
    )
    stage_report = pop_stage_report()
    if preflight_result is not None:
        yield preflight_result
        return
    if stage_report:
        # R-P1-5 四阶段观测帧：rewrite → dense/sparse → rrf → rerank
        # （每阶段耗时/命中数/TopK id/熔断位，供前端与评测归因）
        yield {
            "type": "progress",
            "stage": "retrieval",
            "message": "检索完成（四阶段）",
            "detail": {
                "step": 2,
                "total": 3,
                "stages": stage_report.get("stages", []),
            },
        }

    # --- 7. 正式出答 ---
    yield {
        "type": "progress",
        "stage": "llm_generate",
        "message": "正在生成回答",
        "detail": {"step": 3, "total": 3},
    }
    async for payload in _stream_final_answer(
        session,
        kb_id=kb_id,
        question=question,
        cid=cid,
        user_msg=user_msg,
        request_id=request_id,
        history_text=history_text,
        scope_type=scope_type,
        scope_id=scope_id,
        vectorstore=vectorstore,
        metadata_filter=metadata_filter,
        scope_token=scope_token,
        artifact_id=artifact_id,
        search_k=search_k,
        cache_redis=cache_redis,
        chat_mode=chat_mode,
        model_name=model_name,
    ):
        yield payload


class ChatService:
    def __init__(self, session: Session, cache_redis: redis.Redis | None = None):
        self.session = session
        self.cache_redis = cache_redis

    def stream_answer(
        self,
        *,
        kb_id: str,
        question: str,
        conversation_id: str | None,
        request_id: str,
        user_id: str,
        document_id: str | None = None,
        folder_id: str | None = None,
        chat_mode: ChatMode = "fast",
    ):
        return _stream_answer_impl(
            self.session,
            kb_id=kb_id,
            question=question,
            conversation_id=conversation_id,
            request_id=request_id,
            user_id=user_id,
            cache_redis=self.cache_redis,
            document_id=document_id,
            folder_id=folder_id,
            chat_mode=chat_mode,
        )
