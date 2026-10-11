"""
对话服务辅助函数（从 chat_service.py 拆分）

职责：
- _prepare_context：准备上下文（历史记录、scope标签等）
- _stream_thinking_prelude：流式思考前奏
- _stream_retrieval_preflight：检索预检
- _stream_final_answer：最终回答
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator

import redis
from sqlmodel import Session

from agentix.retrieval.usecases.chat_llm import ChatMode
from agentix.retrieval.usecases.chat_thinking import stream_thinking_prelude
from agentix.retrieval.usecases.chat_scope import (
    scope_prompt_label,
    retrieval_cache_scope_token,
    retrieval_filter_for_question,
)
from agentix.retrieval.usecases.chat_service import (
    _finished_answer_chunk,
    retrieve_scoped_docs_cached,
    should_skip_retrieval_preflight,
)
from agentix.retrieval.infra.repos.conversation_repo import get_recent_history_text
from agentix.retrieval.domain.models.chat import Message

logger = logging.getLogger(__name__)

# 检索未过阈值 / 无命中时的统一拒答
INSUFFICIENT_RETRIEVAL_MSG = "未检索到足够相关内容，无法基于文档回答。"


async def _prepare_context(
    session: Session,
    *,
    cid: str,
    kb_id: str,
    scope_type: str,
    scope_id: str | None,
    chat_mode: ChatMode,
    question: str,
) -> dict:
    """准备对话上下文（历史记录、scope标签等）"""
    history_text = get_recent_history_text(session, cid, limit=10)
    scope_label = scope_prompt_label(
        session, kb_id=kb_id, scope_type=scope_type, scope_id=scope_id
    )
    
    return {
        "history_text": history_text,
        "scope_label": scope_label,
    }


async def _stream_thinking_prelude_if_deep(
    *,
    chat_mode: ChatMode,
    model_name: str,
    question: str,
    history_text: str,
    scope_label: str,
    scope_doc_count: int,
    request_id: str,
) -> AsyncGenerator[dict, None]:
    """流式思考前奏（仅 deep 模式）"""
    if chat_mode != "deep":
        return
    
    async for think_delta in stream_thinking_prelude(
        model_name=model_name,
        question=question,
        history_text=history_text,
        scope_label=scope_label,
        doc_count=scope_doc_count,
    ):
        if think_delta:
            yield {
                "type": "thinking_chunk",
                "content": think_delta,
                "request_id": request_id,
                "finished": False,
            }


async def _stream_retrieval_preflight(
    session: Session,
    *,
    kb_id: str,
    question: str,
    search_k: int,
    scope_type: str,
    scope_id: str | None,
    metadata_filter,
    vectorstore,
    cache_redis: redis.Redis | None,
    artifact_id: str,
    scope_token: str,
    cid: str,
    user_msg: Message,
    model_name: str,
    request_id: str,
    history_text: str = "",
) -> dict | None:
    """检索预检：内容题无命中则拒答；元问题（统计/列表）跳过"""
    if should_skip_retrieval_preflight(question):
        return None
    
    # 问题点名某文档标题时，在 scope 内再收窄到单文件
    pre_meta, pre_named_doc = retrieval_filter_for_question(
        session,
        kb_id=kb_id,
        scope_type=scope_type,
        scope_id=scope_id,
        base_filter=metadata_filter,
        text=question,
    )
    preflight_docs = await retrieve_scoped_docs_cached(
        kb_id=kb_id,
        question=question,
        search_k=search_k,
        metadata_filter=pre_meta,
        vectorstore=vectorstore,
        cache_redis=cache_redis,
        artifact_id=artifact_id,
        scope_token=retrieval_cache_scope_token(scope_token, pre_named_doc),
        session=session,
        history=history_text,
    )
    
    if not preflight_docs:
        return _finished_answer_chunk(
            session,
            conversation_id=cid,
            question_message_id=user_msg.id,
            answer_text=INSUFFICIENT_RETRIEVAL_MSG,
            model_name=model_name,
            request_id=request_id,
        )
    
    return None


async def _stream_final_answer(
    session: Session,
    *,
    kb_id: str,
    question: str,
    cid: str,
    user_msg: Message,
    request_id: str,
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
) -> AsyncGenerator[dict, None]:
    """最终回答：根据 RAG 引擎选择不同的回答方式"""
    from agentix.retrieval.usecases.chat_service import _stream_agent_and_persist
    async for payload in _stream_agent_and_persist(
        session,
            kb_id=kb_id,
            cid=cid,
            user_msg=user_msg,
            question=question,
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
            request_id=request_id,
    ):
        yield payload
