"""P2 可解释（X2）：答案解释 API v1。

``GET /answers/{answer_id}/explanation`` ——聚合**已持久化**的数据回答
"这条答案是怎么来的"：用户问题、检索证据（query/top_k/命中块）、
落库引用、模型名；未持久化的块（路由理由 / citation 校验判定 / grounding
报告 / prompt 版本 / 轨迹）显式 ``not_recorded``，绝不编造。

设计约束（遵守既有纪律）：
- **零 schema 变更**：只读 Answer / RetrievalRun / Message 三张既有表，
  不新增列、不改动落库行为（grounding 报告 v1 仅内存/日志的既有决策不变）；
- 只读端点：不写任何表、不触发任何副作用；
- 缺数据（无检索命中的拒答回答）也是合法解释，返回 ``no_retrieval``。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from sqlmodel import Session, select

from agentix.retrieval.api.deps import DependsSession, get_current_user
from agentix.retrieval.api.response import ErrorCode, success
from agentix.retrieval.domain.models.chat import Message
from agentix.retrieval.domain.models.index import Answer, RetrievalRun
from agentix.retrieval.domain.models.user import User
from agentix.retrieval.errors import AppError

logger = logging.getLogger("agentix.retrieval.explanations")

router = APIRouter(dependencies=[Depends(get_current_user)])

# 证据预览截断长度（hit_chunks.content 落库时已 ≤600，这里再截避免响应过大）
_PREVIEW_LEN = 160
# 证据块返回上限（top_k 可能数十条，解释场景前几条足以说明"证据选择依据"）
_MAX_CHUNKS = 8

_NOT_RECORDED_BLOCKS = {
    # 决策链三块的现状：判定结果只进日志（citation/grounding）或内存
    # （route），轨迹按 trace_id 落文件但与 answer_id 无关联——在建立
    # 持久化关联前显式 not_recorded，绝不编造。
    "route_decision": "路由理由未持久化（仅 trajectory 文件）",
    "citation_verifier": "引用校验判定未持久化（未通过项以答案尾部标注呈现）",
    "grounding": "grounding 报告未持久化（v1 仅内存/日志，见 R3-6 决策）",
    "prompt_version": "prompt 版本未随答案落库",
}


def _not_recorded() -> dict:
    return {"status": "not_recorded"}


def _evidence_block(session: Session, answer: Answer) -> dict:
    """从 RetrievalRun 组装证据选择依据；无检索运行返回 no_retrieval。"""
    run = session.exec(
        select(RetrievalRun)
        .where(
            RetrievalRun.conversation_id == answer.conversation_id,
            RetrievalRun.question_message_id == answer.question_message_id,
        )
        .order_by(RetrievalRun.created_at.desc())  # type: ignore[union-attr]
    ).first()
    if run is None:
        return {"status": "no_retrieval"}

    chunks = run.hit_chunks or []
    return {
        "status": "recorded",
        "retrieval_run": {
            "query_text": run.query_text,
            "top_k": run.top_k,
            "hit_count": len(chunks),
            "chunks": [
                {
                    "chunk_id": c.get("chunk_id"),
                    "score": c.get("score"),
                    "source": c.get("source"),
                    "effective_status": c.get("effective_status"),
                    "replaced_by": c.get("replaced_by"),
                    "content_preview": (c.get("content") or "")[:_PREVIEW_LEN],
                }
                for c in chunks[:_MAX_CHUNKS]
            ],
        },
    }


@router.get("/answers/{answer_id}/explanation")
def api_get_answer_explanation(
    request: Request,
    answer_id: str,
    session: Session = DependsSession,
    current_user: User = Depends(get_current_user),
):
    """答案解释：聚合问题/证据/引用/模型等已持久化事实（X2 v1）。"""
    answer = session.get(Answer, answer_id)
    if answer is None:
        raise AppError(
            error_code=ErrorCode.ANSWER_NOT_FOUND,
            message=f"answer {answer_id} not found",
            status_code=404,
            user_hint="答案不存在或已被删除",
        )

    question_msg = session.get(Message, answer.question_message_id)

    return success(
        data={
            "answer": {
                "id": answer.id,
                "conversation_id": answer.conversation_id,
                "question_message_id": answer.question_message_id,
                "version": answer.version,
                "is_current": answer.is_current,
                "created_at": answer.created_at.isoformat() if answer.created_at else None,
            },
            "question": {
                "message_id": answer.question_message_id,
                "content": question_msg.content if question_msg else None,
            },
            "model": {
                "name": answer.model_name,
                "prompt_version": _not_recorded(),
            },
            "evidence": _evidence_block(session, answer),
            "citations": answer.citations or [],
            "validation": {k: _not_recorded() for k in _NOT_RECORDED_BLOCKS},
            "notes": (
                "X2 v1 仅聚合已持久化数据；validation 各块显式 not_recorded，"
                "详见 docs/testing.md 与模块 docstring"
            ),
        },
        request_id=request.state.request_id,
    )
