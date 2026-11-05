"""P2-1 满意度反馈端点（POST /api/v1/feedback）。

设计要点（架构设计定稿，见 M-3 修正）：

- **用户面而非 ops 面**：反馈带"这是谁对哪条消息的评价"语义，归属纪律
  沿 v1 用户面（``require_v1_user`` + ``resolve_v1_caller`` +
  ``session_visible_to`` 跨用户 404 防存在性枚举）；ops 面是机器面
  ``verify_api_key``，没有用户归属概念，放那里语义就错了。
- **落点复用 EventPort**：事件类型 ``message_feedback``，与
  ``user_notification`` 同模式直接 publish（事件即 dict，无需注册表）；
  查询走既有 ``GET /api/v1/ops/events?event_type=message_feedback``。
- **flag-off 路由不注册**（``FEEDBACK_ENABLED`` 默认 False）：聚合点
  （``v1/__init__.py``）按 flag 决定是否 include，flag-off 与基线逐位等价。
- **fail-open**：EventPort 发布失败只降级为 503 语义记录 + 打日志，
  绝不把反馈失败放大成请求链路故障（反馈是旁路数据）。
- **PII 脱敏默认开**（``FEEDBACK_PII_REDACT``）：reason 复用记忆模块的
  ``redact_pii``（中英姓名/地址/电话），脱敏是安全项。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from agentix.config import settings
from agentix.infrastructure.core.database import get_db
from agentix.interfaces.http.v1.deps import (
    resolve_v1_caller,
    require_v1_user,
    session_visible_to,
)

router = APIRouter(dependencies=[Depends(require_v1_user)])

#: 事件类型（与 ops 查询、导出脚本约定同一字符串）
MESSAGE_FEEDBACK_EVENT_TYPE = "message_feedback"


class FeedbackRequest(BaseModel):
    """反馈请求体：rating 必填且有界；message_id/trace_id 至少给一个便于归因。"""

    session_id: str = Field(..., min_length=1, description="会话 ID")
    rating: Literal["up", "down"] = Field(..., description="点赞/点踩（有界枚举）")
    message_id: Optional[str] = Field(None, description="被评价的消息 ID（可选）")
    trace_id: Optional[str] = Field(None, description="被评价请求的追踪 ID（可选）")
    reason: Optional[str] = Field(None, max_length=2000, description="点踩原因（选填）")


class FeedbackResponse(BaseModel):
    """反馈回执：queued 表示事件已入 EventPort。"""

    status: str = "queued"
    event_type: str = MESSAGE_FEEDBACK_EVENT_TYPE
    event_id: str


class FeedbackStatusResponse(BaseModel):
    """探活响应：路由可达即 enabled——本路由整体仅在
    ``FEEDBACK_ENABLED=true`` 时注册（flag-off 时这里是 404，
    前端探活 fail-open 返回 false = 不渲染反馈按钮）。"""

    enabled: bool = True


@router.post("", response_model=FeedbackResponse)
@router.post("/", response_model=FeedbackResponse, include_in_schema=False)
async def submit_feedback(
    request: FeedbackRequest,
    db: Session = Depends(get_db),
    caller: Optional[str] = Depends(resolve_v1_caller),
) -> FeedbackResponse:
    """提交一条消息反馈（点赞/点踩）→ EventPort ``message_feedback``。

    归属校验：会话存在且 caller 已声明身份时，跨用户提交回 404
    （沿 ``chat._ensure_session_visible`` 同款语义，防存在性枚举）。
    """
    from agentix.application.services.chat_service import get_chat_service

    # 归属校验（与 chat 写路径同款：不存在保持既有语义，跨用户 404）
    chat_service = get_chat_service()
    if caller is not None and chat_service.session_exists(db, request.session_id):
        owner = chat_service.get_session_owner(db, request.session_id)
        if not session_visible_to(owner, caller):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found",
            )

    # PII 脱敏（默认开；脱敏原语复用记忆模块，单一事实源）
    reason = request.reason or ""
    if reason and settings.FEEDBACK_PII_REDACT:
        from agentix.agent.memory.service import redact_pii

        reason = redact_pii(reason)

    payload = {
        "message_id": request.message_id,
        "trace_id": request.trace_id,
        "session_id": request.session_id,
        "rating": request.rating,
        "reason": reason,
        "user": caller,
        # 事件时间戳是数据面 → aware UTC（p42 时间禁止 naive 时钟；
        # 展示口径由读取方按 DISPLAY_TZ 换算）
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    try:
        from agentix.agent.events.port import get_event_port
        from agentix.observability.metrics import record_feedback

        event_id = await get_event_port().publish(
            MESSAGE_FEEDBACK_EVENT_TYPE, payload
        )
        # 打点 fail-open（指标故障不影响反馈入队）
        try:
            record_feedback(request.rating)
        except Exception:  # noqa: BLE001 - 指标故障不影响主流程
            pass
    except Exception as exc:  # noqa: BLE001 - 反馈失败不影响主链路（fail-open）
        logger.warning("feedback event publish failed (non-fatal): {}", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Feedback temporarily unavailable",
        ) from exc

    return FeedbackResponse(event_id=event_id)


@router.get("/status", response_model=FeedbackStatusResponse)
async def feedback_status() -> FeedbackStatusResponse:
    """前端探活（P2-1 接线）：GET /api/v1/feedback/status → {"enabled": true}。

    flag-on 时路由可达返回 200；flag-off 时路由未注册 → 404 →
    前端 ``probeFeedbackEnabled`` fail-open 不渲染。鉴权沿 router 级
    ``require_v1_user``：鉴权失败同样非 200，前端一致 fail-open。
    """
    return FeedbackStatusResponse()
