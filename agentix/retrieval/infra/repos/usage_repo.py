"""
用量记录 Repo（运营 / 成本，一期：chat 流式问答）
"""
from __future__ import annotations

import logging

from sqlmodel import Session, select

from agentix.retrieval.domain.models.ops import UsageRecord

logger = logging.getLogger(__name__)


def record_chat_stream_usage(
    session: Session,
    *,
    request_id: str,
    kb_id: str | None,
    user_id: str | None,
    chat_mode: str,
    model_name: str,
    latency_ms: int,
    error_code: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
) -> UsageRecord | None:
    """写入一次 chat/stream 用量；失败时打日志，不影响主流程。"""
    try:
        row = UsageRecord(
            request_id=request_id,
            kb_id=kb_id,
            user_id=user_id,
            action=f"chat.stream.{chat_mode}",
            latency_ms=max(0, int(latency_ms)),
            tokens_in=max(0, int(tokens_in)),
            tokens_out=max(0, int(tokens_out)),
            cost=0.0,
            model_name=model_name or "",
            error_code=error_code,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row
    except Exception as e:
        logger.warning("record_chat_stream_usage failed: %s", e)
        try:
            session.rollback()
        except Exception as rb_exc:
            # rollback 本身也失败 ⇒ 会话/连接状态已不可信（不是"某条插入失败"
            # 这种局部问题）。外层 warning 只说"记账失败"，会把严重度说低，
            # 因此这里必须单独留痕，且要点明后果：该 request 的用量**永久缺失**，
            # 后续复用同一 session 的写入也会连带失败。
            logger.error(
                "record_chat_stream_usage rollback also failed; session is unusable, "
                "usage for request_id=%s is permanently missing: %s",
                request_id,
                rb_exc,
                exc_info=True,
            )
        return None


def get_usage_by_request_id(session: Session, request_id: str) -> UsageRecord | None:
    stmt = select(UsageRecord).where(UsageRecord.request_id == request_id).limit(1)
    return session.exec(stmt).first()
