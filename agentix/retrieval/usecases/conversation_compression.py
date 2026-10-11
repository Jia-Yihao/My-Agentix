"""
M3.5: 长会话摘要压缩。
- 消息数超 CONVERSATION_SUMMARY_THRESHOLD 时，将最早轮次压缩为摘要；
- 保留最近 CONVERSATION_KEEP_RECENT_ROUNDS 轮原文；
- 压缩在回答之后执行，不阻断流式回答；
- 压缩失败降级：保留未压缩原始历史，记录 ERROR，下次再试。
"""
from __future__ import annotations

import logging

from sqlmodel import Session

from agentix.retrieval.infra.repos.conversation_repo import get_conversation_by_id, list_messages_for_compression
from agentix.retrieval.settings import settings

logger = logging.getLogger(__name__)

_SUMMARY_PROMPT = """请将以下对话历史压缩为简洁摘要，保留：
1. 用户明确表达的偏好和要求（如「请用中文回答」「只看最新版本」）
2. 已确认的核心事实和结论
3. 关键上下文（讨论的主题、涉及的文档）

对话历史：
{history}

摘要（不超过 300 字）："""


async def maybe_compress_conversation(
    session: Session,
    *,
    conversation_id: str,
    model_name: str = "",
) -> None:
    """
    检查并执行会话压缩。在回答落库后调用，不阻断流式回答。

    - threshold=0 时不压缩（默认）；
    - 消息数 <= threshold 时不压缩；
    - 压缩失败时保留原始历史，仅记录 ERROR。
    """
    threshold = settings.CONVERSATION_SUMMARY_THRESHOLD
    if threshold <= 0:
        return

    keep_recent = max(2, settings.CONVERSATION_KEEP_RECENT_ROUNDS * 2)  # 每轮 = user + assistant

    conv = get_conversation_by_id(session, conversation_id)
    if not conv:
        return

    messages = list_messages_for_compression(session, conversation_id)
    if len(messages) <= threshold:
        return

    # 分割：旧消息（待压缩）+ 近期消息（保留原文）
    old_messages = messages[:-keep_recent]
    if not old_messages:
        return

    # 构建待压缩文本
    history_lines = []
    for msg in old_messages:
        role_label = "用户" if msg.role == "user" else "助手"
        history_lines.append(f"{role_label}: {msg.content}")
    history_text = "\n".join(history_lines)

    # 调用 LLM 生成摘要
    try:
        summary = await _generate_summary(history_text, model_name)
    except Exception as e:
        logger.error(
            "conversation compression failed, keeping original history: %s",
            e,
            extra={"conversation_id": conversation_id, "stage": "compress"},
        )
        return

    if not summary or not summary.strip():
        logger.warning("compression produced empty summary, keeping original history")
        return

    # 合并已有摘要 + 新摘要
    if conv.summary:
        new_summary = f"{conv.summary}\n\n[后续摘要]\n{summary.strip()}"
    else:
        new_summary = summary.strip()

    # 落库：更新摘要 + 删除旧消息
    try:
        conv.summary = new_summary
        conv.updated_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        session.add(conv)
        for msg in old_messages:
            session.delete(msg)
        session.commit()
        logger.info(
            "conversation compressed: %d old messages → summary (%d chars)",
            len(old_messages),
            len(new_summary),
            extra={"conversation_id": conversation_id, "stage": "compress"},
        )
    except Exception as e:
        logger.error(
            "conversation compression persist failed, rolling back: %s",
            e,
            extra={"conversation_id": conversation_id, "stage": "persist"},
        )
        session.rollback()


async def _generate_summary(history_text: str, model_name: str) -> str:
    """调用 LLM 生成摘要。"""
    from agentix.retrieval.usecases.chat_llm import create_chat_llm, invoke_with_breaker

    prompt = _SUMMARY_PROMPT.format(history=history_text)
    llm = create_chat_llm(model_name=model_name or "default", chat_mode="fast")
    resp = await invoke_with_breaker(llm, prompt)
    text = getattr(resp, "content", None) or str(resp)
    return text.strip()
