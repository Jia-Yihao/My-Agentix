"""
对话问答路由（SSE 流式响应）
"""
import json
import logging
from typing import Optional

from fastapi import APIRouter, Request, Depends
from fastapi.responses import StreamingResponse

from agentix.retrieval.api.deps import get_chat_service, get_current_user
from agentix.retrieval.domain.models.user import User
from agentix.retrieval.api.response import success, ErrorCode, get_status_code, ERROR_CODE_TO_HINT
from agentix.retrieval.api.schemas.chat import ChatRequest
from agentix.retrieval.errors import AppError
from agentix.reliability.rate_limiter import RateLimiter
from agentix.retrieval.settings import settings
from agentix.retrieval.usecases.chat_service import ChatService

router = APIRouter(dependencies=[Depends(get_current_user)])

logger = logging.getLogger("agentix.retrieval.chat")

# 令牌桶为进程级单例；RATE_LIMIT_ENABLED=false（默认）时依赖直通，零开销
# 审计批次 A3（2026-09-28）：原 import 期一次性绑定 settings.RATE_LIMIT_RATE /
# BURST——改配置须重启进程才生效。改为惰性派生：签名（rate, burst）不变时复用
# 实例，变化时热重建，运行期改配置即时生效。
_chat_limiter: Optional[RateLimiter] = None
_chat_limiter_signature: Optional[tuple] = None


def _get_chat_limiter() -> RateLimiter:
    """按 settings 派生限流器（审计批次 A3）：同签名复用，签名变化热重建。"""
    global _chat_limiter, _chat_limiter_signature
    sig = (settings.RATE_LIMIT_RATE, settings.RATE_LIMIT_BURST)
    if _chat_limiter is None or _chat_limiter_signature != sig:
        _chat_limiter = RateLimiter(rate=sig[0], burst=sig[1])
        _chat_limiter_signature = sig
    return _chat_limiter


async def _chat_rate_limit() -> None:
    """chat 流式路由限流 dependency（T18）：默认关（RATE_LIMIT_ENABLED=false）。"""
    if not settings.RATE_LIMIT_ENABLED:
        return
    if not await _get_chat_limiter().acquire():
        raise AppError(
            error_code=ErrorCode.RATE_LIMITED,
            message="请求过于频繁，请稍后再试",
            status_code=get_status_code(ErrorCode.RATE_LIMITED),
            user_hint=ERROR_CODE_TO_HINT.get(ErrorCode.RATE_LIMITED),
        )


@router.post("/chat")
def api_chat(
    request: Request,
    req: ChatRequest,
    current_user: User = Depends(get_current_user),
    svc: ChatService = Depends(get_chat_service),
):
    """非流式问答（将流式输出聚合为一次性返回）"""
    request_id = request.state.request_id

    uid = current_user.id

    async def _run() -> dict:
        chunks: list[str] = []
        final_message_id = ""
        final_conversation_id = req.conversation_id
        final_answer_id = ""
        citations = []
        async for payload in svc.stream_answer(
            kb_id=req.kb_id,
            question=req.question,
            conversation_id=req.conversation_id,
            request_id=request_id,
            user_id=uid,
            document_id=req.document_id,
            folder_id=req.folder_id,
            chat_mode=req.chat_mode,
        ):
            if payload.get("type") == "error":
                code = payload.get("code") or ErrorCode.INTERNAL_ERROR
                raise AppError(
                    error_code=str(code),
                    message=str(payload.get("content") or "处理失败"),
                    status_code=get_status_code(str(code)),
                    user_hint=ERROR_CODE_TO_HINT.get(str(code)),
                )
            if payload.get("type") == "answer_chunk":
                chunks.append(payload.get("content", ""))
                if payload.get("finished"):
                    final_message_id = payload.get("message_id", "") or ""
                    final_conversation_id = payload.get("conversation_id") or final_conversation_id
                    final_answer_id = payload.get("answer_id", "") or ""
                    citations = payload.get("citations", []) or []
                    break
        return {
            "conversation_id": final_conversation_id,
            "message_id": final_message_id,
            "answer_id": final_answer_id,
            "answer": "".join(chunks),
            "citations": citations,
        }

    import anyio

    data = anyio.run(_run)
    return success(data=data, request_id=request_id)


@router.post("/chat/stream", dependencies=[Depends(_chat_rate_limit)])
def api_chat_stream(
    request: Request,
    req: ChatRequest,
    current_user: User = Depends(get_current_user),
    svc: ChatService = Depends(get_chat_service),
):
    """流式问答 (SSE)"""
    request_id = request.state.request_id

    async def sse_generator():
        # 接口审查批次 2026-09-28：生成器原无异常保护——meta/progress 已发出
        # （HTTP 头已提交）后再 raise，流会被硬截断且前端收不到任何终态帧，
        # 半截回答被当成完成。现收口为结构化 error 帧后正常结束。
        # （GeneratorExit 走 BaseException，不在此捕获——断连不许再 yield。）
        try:
            from agentix.application.services.stream_gate import gate_events

            # Phase H·W4：SSE 出口门控（token 合并/背压，flag 关=原样直通）。
            # 非流式聚合（上方 _run）不经此缝——背压丢帧只作用于真 SSE。
            async for payload in gate_events(
                svc.stream_answer(
                    kb_id=req.kb_id,
                    question=req.question,
                    conversation_id=req.conversation_id,
                    request_id=request_id,
                    user_id=current_user.id,
                    document_id=req.document_id,
                    folder_id=req.folder_id,
                    chat_mode=req.chat_mode,
                )
            ):
                # A5/R5b（SSE 词汇归一）：type 为唯一词汇；B2 的 event 双写
                # 兜底块已删除（前端 parser 自 B2 起已容错两词）。
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "retrieval chat stream aborted (non-fatal): request_id={} error={}",
                request_id,
                exc,
            )
            err = {
                "type": "error",
                "code": ErrorCode.INTERNAL_ERROR,
                "content": "处理请求时出错，请稍后重试。",
            }
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

    return StreamingResponse(sse_generator(), media_type="text/event-stream")
