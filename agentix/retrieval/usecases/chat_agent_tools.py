"""
对话 Agent 工具：范围由后端注入，模型只选择调用哪条工具。
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import redis
from langchain_core.tools import tool
from sqlmodel import Session

from agentix.retrieval.domain.policies.errors import sanitize_error
from agentix.observability.spans import record_model_used, span
from agentix.reliability.circuit_breaker import CircuitBreakerOpenError
from agentix.retrieval.settings import settings

from agentix.retrieval.usecases.chat_scope import (
    count_documents_in_scope,
    document_title_for_id,
    list_document_titles_in_scope,
    resolve_document_id_from_question,
    retrieval_cache_scope_token,
    retrieval_filter_for_question,
    scope_prompt_label,
)

logger = logging.getLogger(__name__)


@dataclass
class AgentToolContext:
    session: Session
    kb_id: str
    scope_type: str
    scope_id: str | None
    vectorstore: Any
    metadata_filter: dict | Any | None
    search_k: int
    cache_redis: redis.Redis | None
    artifact_id: str
    scope_token: str
    model_name: str
    chat_mode: str = "fast"
    last_citations: list[dict] = field(default_factory=list)
    last_hit_chunks: list[dict] = field(default_factory=list)
    last_contexts: list[str] = field(default_factory=list)
    # R3-5 Context Engine：最近一次 context 装配统计（ranking/budget/isolation）
    last_context_meta: dict = field(default_factory=dict)
    # R3-6 GroundingValidator：最近一次答案锚定校验报告（观测模式）
    last_grounding: dict = field(default_factory=dict)
    # R-P2-2 证据门禁：最近一次判定（mode/action/triggers，仅内存审计）
    last_evidence_gate: dict = field(default_factory=dict)


# 工具描述单一数据源：desc 正向描述 + anti 反例（不适合场景）。
# system prompt 的工具列表与每个 @tool 的 description 都由它生成，避免漂移。
TOOL_SPECS: dict[str, dict[str, str]] = {
    "count_scope_documents": {
        "desc": "统计当前对话范围内有多少个文档/文件。用于「有几个文件」「多少份文档」等问题。",
        "anti": "需要文档内容、摘要、原文片段或具体信息的问题（用 query_knowledge_base 或 search_documents）。",
    },
    "list_scope_documents": {
        "desc": "列出当前范围内的文档标题（最多 20 条）。用于「有哪些文件」「列一下文档」。",
        "anti": "判断某个具体文件名是否存在（用 lookup_scope_document）、问内容或搜关键词。",
    },
    "lookup_scope_document": {
        "desc": "按名称在当前范围内查找某个文档是否存在（完整扫描，不限 20 条）。",
        "anti": "问文档内容、摘要、列所有文件，或搜关键词原文（用 search_documents）。",
    },
    "query_knowledge_base": {
        "desc": "基于文档内容理解并回答问题（解释、总结、主要内容）。",
        "anti": "用户明确要搜某个具体关键词的原文片段（用 search_documents）；也不适合问数量/列文件。",
    },
    "search_documents": {
        "desc": "按关键词在文档中检索原文片段（向量相似度）。用户明确说「搜索/查找/找某个词」时用。",
        "anti": "需要理解、总结、解释类问题（用 query_knowledge_base）；也不适合问数量/列文件。",
    },
}


def build_agent_system_prompt(ctx: AgentToolContext) -> str:
    """
    静态前缀：角色 / 能力 / 工具规范 / 规则。
    不随会话变化——动态范围信息（KB/文件夹/文档）经独立通道注入（用户消息前缀），
    不内嵌 system prompt（M3.2）。
    """
    tool_lines = "\n".join(
        f"{i}. **{name}** — {spec['desc']}"
        for i, (name, spec) in enumerate(TOOL_SPECS.items(), 1)
    )
    return f"""你是 My Agentix 智能文档助手。当前对话已锁定范围，不可切换知识库或文件夹。

## 工具（按问题类型选择，勿向用户暴露工具名）
{tool_lines}

## 规则
- 问数量 → count_scope_documents；问有哪些文件 → list_scope_documents
- 用户问**某个具体文档**的内容、摘要、页码等 → **直接** query_knowledge_base，不要先用 list 判断是否存在
- list_scope_documents 最多显示 20 条，**不能**据此断定某个文件名不存在
- 若 query_knowledge_base 已返回正文，最终回复必须基于该内容，**禁止**再说「未找到该文档」
- 仅当用户要搜具体关键词时用 search_documents
- 回答使用中文，简洁准确；若工具无结果，如实说明
- 凡内容类问题必须先调 query_knowledge_base，禁止不查资料直接答
"""


def rag_authority_ranking_enabled(docs: list) -> bool:
    """主链 RAG 的 authority 排序开关决策（仅服务 ``query_knowledge_base``）。

    本批片段确实经过精排（带 ``_rerank_score``）且开关打开时跳过 authority 重排：
    CE 分数是 query-specific 的相关性，而 ``source_authority_level`` 是与本次问题
    无关的全局档位，重排会把高相关低档位片段推到 token 预算之外。开关关闭或精排
    fail-open（无标记）时返回值与原实现一致，逐位等价。
    """
    if not settings.CONTEXT_AUTHORITY_RANKING_ENABLED:
        return False
    if not (
        settings.CONTEXT_AUTHORITY_RANKING_SKIP_ON_RERANK
        and settings.RETRIEVAL_RERANK_ENABLED
    ):
        return True
    reranked = any(
        (getattr(doc, "metadata", None) or {}).get("_rerank_score") is not None
        for doc in docs
    )
    return not reranked


def build_chat_agent_tools(ctx: AgentToolContext) -> list:
    """构建闭包注入范围后的工具列表。"""

    @tool
    def count_scope_documents() -> str:
        """统计当前对话范围内有多少个文档/文件。用于「有几个文件」「多少份文档」等问题。"""
        n = count_documents_in_scope(
            ctx.session,
            kb_id=ctx.kb_id,
            scope_type=ctx.scope_type,
            scope_id=ctx.scope_id,
        )
        label = scope_prompt_label(
            ctx.session,
            kb_id=ctx.kb_id,
            scope_type=ctx.scope_type,
            scope_id=ctx.scope_id,
        )
        return f"{label}内共有 {n} 个文档。"

    @tool
    def list_scope_documents(limit: int = 20) -> str:
        """列出当前范围内的文档标题。limit 为最多返回条数（默认 20）。"""
        cap = max(1, min(int(limit or 20), 50))
        titles = list_document_titles_in_scope(
            ctx.session,
            kb_id=ctx.kb_id,
            scope_type=ctx.scope_type,
            scope_id=ctx.scope_id,
            limit=cap,
        )
        if not titles:
            return "当前范围内没有文档。"
        lines = "\n".join(f"- {t}" for t in titles)
        return (
            f"共 {len(titles)} 个文档（最多显示 {cap} 个，可能未包含全部文件）：\n{lines}\n"
            "提示：判断某个文件名是否存在请用 lookup_scope_document，问文档内容请用 query_knowledge_base。"
        )

    @tool
    def lookup_scope_document(name_hint: str) -> str:
        """在当前范围内按名称查找文档是否存在（扫描全部文档，不受 list 条数限制）。"""
        hint = (name_hint or "").strip()
        if not hint:
            return "请提供要查找的文档名称。"
        doc_id = resolve_document_id_from_question(
            ctx.session,
            kb_id=ctx.kb_id,
            scope_type=ctx.scope_type,
            scope_id=ctx.scope_id,
            text=hint,
        )
        if not doc_id:
            return f"在当前范围内未找到名称匹配「{hint}」的文档。"
        title = document_title_for_id(ctx.session, doc_id) or doc_id
        return f"已找到文档：{title}"

    @tool
    async def query_knowledge_base(question: str) -> str:
        """基于当前范围内已索引的文档内容回答问题。适合需要理解、解释、总结的问题。"""
        from agentix.retrieval.usecases.chat_service import (
            INSUFFICIENT_RETRIEVAL_MSG,
            build_prompt,
            get_citations_from_docs,
            hit_chunks_from_docs,
            retrieve_scoped_docs_cached,
        )
        from agentix.retrieval.usecases.chat_llm import (
            create_chat_llm,
            get_default_config,
            invoke_with_breaker,
            thinking_extra_body,
        )

        q = (question or "").strip()
        if not q:
            return "问题不能为空。"

        meta_filter, named_doc_id = retrieval_filter_for_question(
            ctx.session,
            kb_id=ctx.kb_id,
            scope_type=ctx.scope_type,
            scope_id=ctx.scope_id,
            base_filter=ctx.metadata_filter,
            text=q,
        )
        docs = await retrieve_scoped_docs_cached(
            kb_id=ctx.kb_id,
            question=q,
            search_k=ctx.search_k,
            metadata_filter=meta_filter,
            vectorstore=ctx.vectorstore,
            cache_redis=ctx.cache_redis,
            artifact_id=ctx.artifact_id,
            scope_token=retrieval_cache_scope_token(ctx.scope_token, named_doc_id),
            session=ctx.session,
        )
        if not docs:
            return INSUFFICIENT_RETRIEVAL_MSG

        # R3-5 Context Engine：ranking（authority）→ isolation → budget 统一装配。
        # 本批经过精排时按开关跳过全局 authority 重排，让 CE 相关性顺序直接进预算。
        from agentix.retrieval.domain.services.context_engine import (
            assemble_context,
            context_doc_key,
        )

        ranking_enabled = rag_authority_ranking_enabled(docs)
        context_str, context_meta = assemble_context(
            docs,
            ranking_enabled=ranking_enabled,
            budget_tokens=settings.CONTEXT_BUDGET_TOKENS,
            isolate_threshold=settings.CONTEXT_ISOLATE_THRESHOLD,
            source="query_knowledge_base",
            truncate_ratio=settings.CONTEXT_BUDGET_TRUNCATE_RATIO,
        )
        if not ranking_enabled and settings.CONTEXT_AUTHORITY_RANKING_ENABLED:
            context_meta["authority_ranking_skipped_for_rerank"] = True
        ctx.last_context_meta = context_meta

        # 预算裁剪后只把「模型真正看过的片段」暴露给引用卡、命中记录与评测 contexts，
        # 否则会出现引用了未进 Prompt 的文本、faithfulness 拿全量片段评估的失真。
        # budget<=0 或未发生裁剪时 kept_doc_keys 覆盖全量，下列三处逐位不变。
        budget = context_meta.get("budget") or {}
        trimmed = bool(budget.get("dropped") or budget.get("truncated"))
        kept_keys = set(context_meta.get("kept_doc_keys") or ())
        if trimmed and kept_keys:
            visible = [d for d in docs if context_doc_key(d) in kept_keys]
            evidence_docs = visible or docs
        else:
            evidence_docs = docs

        ctx.last_citations = await asyncio.to_thread(
            get_citations_from_docs, evidence_docs, ctx.session
        )
        ctx.last_hit_chunks = hit_chunks_from_docs(evidence_docs)
        ctx.last_contexts = (
            list(context_meta["kept_texts"])
            if trimmed and context_meta.get("kept_texts")
            else [d.page_content for d in docs]
        )

        target_title = document_title_for_id(ctx.session, named_doc_id)
        prompt = build_prompt(
            "",
            context_str,
            q,
            target_document=target_title,
            status_fields={
                "scope": ctx.scope_type,
                "retrieved": len(docs),
                "final_chunks": context_meta.get("final_docs"),
            },
        )
        try:
            async with span("rag.generate", kb_id=ctx.kb_id, tool="query_knowledge_base"):
                # R3-7 Model Gateway（§44）：fallback 链非空时启用跨模型兜底
                # （primary 失败 → 链内下一模型，硬上限防成本爆炸）。
                # 默认 LLM_FALLBACK_MODELS="" → 链为空 → 原路径逐位一致。
                from agentix.infrastructure.llm.model_gateway import get_model_gateway

                gateway = get_model_gateway()
                # P1-1 跨端点优先：LLM_FALLBACK_MODEL_SPECS 非空时按条目自带
                # base_url/api_key 构造 client（本地 LM Studio 等备用端点）；
                # 否则回退旧键 LLM_FALLBACK_MODELS（同端点换模型名）。
                specs = gateway.fallback_specs(primary_model=ctx.model_name)
                if specs:

                    def _build_fallback_client(item):
                        # ainvoke_with_fallback 的链 = [primary(str), *fallbacks]，
                        # primary 是纯模型名（走主链路同构路径）。
                        if isinstance(item, str):
                            return create_chat_llm(model_name=item, chat_mode=ctx.chat_mode)
                        if item.base_url and item.api_key:
                            # 跨端点条目：直接按规格构造（api_key 来自
                            # api_key_env 环境变量，与主链路密钥隔离）
                            from langchain_openai import ChatOpenAI

                            cfg = get_default_config()
                            return ChatOpenAI(
                                model=item.model,
                                temperature=cfg.get("temperature", 0.1),
                                timeout=cfg.get("llm_timeout", 30),
                                max_retries=cfg.get("llm_max_retries", 3),
                                api_key=item.api_key,
                                base_url=item.base_url,
                                extra_body=thinking_extra_body(ctx.chat_mode),
                            )
                        # 仅模型名条目（base_url/api_key 任一为空）：与主链路同构
                        return create_chat_llm(model_name=item.model, chat_mode=ctx.chat_mode)

                    gw_result = await gateway.ainvoke_with_fallback(
                        prompt,
                        primary_model=ctx.model_name,
                        fallback_models=specs,
                        build_client=_build_fallback_client,
                        invoke=lambda client: invoke_with_breaker(
                            client, prompt, name="llm.chat"
                        ),
                    )
                    resp = gw_result.response
                    # 回退后实际服务方上 span（model_used 产出后一直无人
                    # 消费）。主模型名只从 ctx.model_name 取——与上方调用点同参数，
                    # 不另经 settings/resolve_model 造第二判定源（约定 6 ④）。
                    record_model_used(gw_result.model_used, primary_model=ctx.model_name)
                else:
                    fallbacks = gateway.fallback_chain(primary_model=ctx.model_name)
                    if fallbacks:

                        def _build_fallback_client(model: str):
                            # 与主链路同构：同 base_url/extra_body/参数
                            return create_chat_llm(model_name=model, chat_mode=ctx.chat_mode)

                        gw_result = await gateway.ainvoke_with_fallback(
                            prompt,
                            primary_model=ctx.model_name,
                            fallback_models=fallbacks,
                            build_client=_build_fallback_client,
                            invoke=lambda client: invoke_with_breaker(
                                client, prompt, name="llm.chat"
                            ),
                        )
                        resp = gw_result.response
                        # 同分支①——实际服务方上 span，主模型名同源
                        # ctx.model_name（约定 6 ④）；无 fallback 的原路径无 model_used
                        # 可报，不调（规格口径）。
                        record_model_used(gw_result.model_used, primary_model=ctx.model_name)
                    else:
                        llm = create_chat_llm(model_name=ctx.model_name, chat_mode=ctx.chat_mode)
                        resp = await invoke_with_breaker(llm, prompt)
            text = getattr(resp, "content", None) or str(resp)
            text = text.strip() or "未能生成回答。"
            if target_title:
                return f"【文档：{target_title}】\n{text}"
            return text
        except CircuitBreakerOpenError as e:
            logger.warning(
                "llm circuit breaker open",
                extra={"tool": "query_knowledge_base", "status": "open"},
            )
            return f"生成回答时出错，请稍后重试。（{sanitize_error(e)}）"
        except Exception as e:
            logger.exception("query_knowledge_base LLM failed: %s", e)
            return "生成回答时出错，请稍后重试。"

    @tool
    async def search_documents(keyword: str) -> str:
        """在当前范围内按关键词检索文档原文片段（向量相似度）。适合「搜索/查找/找某个词」。"""
        from agentix.retrieval.usecases.chat_service import (
            get_citations_from_docs,
            hit_chunks_from_docs,
            retrieve_docs_with_retry,
        )

        kw = (keyword or "").strip().strip("\"'“”‘’")
        if not kw:
            return "关键词不能为空。"

        meta_filter, named_doc_id = retrieval_filter_for_question(
            ctx.session,
            kb_id=ctx.kb_id,
            scope_type=ctx.scope_type,
            scope_id=ctx.scope_id,
            base_filter=ctx.metadata_filter,
            text=kw,
        )
        try:
            async with span("rag.search", kb_id=ctx.kb_id, tool="search_documents"):
                docs = await asyncio.to_thread(
                    retrieve_docs_with_retry,
                    ctx.vectorstore,
                    kw,
                    search_k=ctx.search_k,
                    metadata_filter=meta_filter,
                )
        except Exception as e:
            logger.exception("search_documents failed: %s", e)
            return f"搜索失败：{sanitize_error(e)}"

        if not docs:
            return f"未找到与「{kw}」相关的文档片段。"

        ctx.last_citations = await asyncio.to_thread(
            get_citations_from_docs, docs, ctx.session
        )
        ctx.last_hit_chunks = hit_chunks_from_docs(docs)
        ctx.last_contexts = [d.page_content for d in docs]

        parts: list[str] = []
        for i, doc in enumerate(docs[:5], 1):
            source = doc.metadata.get("source", "未知来源")
            content = (doc.page_content or "").strip()
            highlighted = re.sub(
                re.escape(kw),
                f"【{kw}】",
                content,
                flags=re.IGNORECASE,
            )
            parts.append(f"[{i} - {source}]\n{highlighted}")
        return f"找到 {len(docs)} 条相关片段（显示前 {min(5, len(docs))} 条）：\n\n" + "\n\n".join(
            parts
        )

    tools = [
        count_scope_documents,
        list_scope_documents,
        lookup_scope_document,
        query_knowledge_base,
        search_documents,
    ]
    # 描述单一数据源：正向描述 + 反例，避免 prompt 与工具描述漂移
    for fn in tools:
        spec = TOOL_SPECS.get(fn.name)
        if spec:
            fn.description = f"{spec['desc']}\n\n不适合：{spec['anti']}"
    return tools
