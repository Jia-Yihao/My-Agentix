"""外部事件 webhook（Phase 20 规划 §8.2）：``POST /events`` → ``EventPort.publish``。

安全边界：

- 鉴权：端点级 ``Depends(verify_api_key)``（``REQUIRE_API_KEY`` 开启时校验
  ``x-api-key``，与全局中间件同口径、为未挂中间件的装配面兜底）+
  应用级全局限流（见 ``rate_limit_middleware``）；
- ``event_type`` 拒绝控制字符（日志/prompt 注入面，声明期挡住）；
- payload 有界（``MAX_PAYLOAD_CHARS``，超限 413）——事件日志不能被撑爆；
- 事件内容是**不可信输入**：永远按数据处理，不拼 prompt（三防线之证据≠指令）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException

from agentix.infrastructure.core.security import verify_api_key
from pydantic import BaseModel, Field as PydField

#: 机器面：接线 verify_api_key（REQUIRE_API_KEY 开启时校验 x-api-key）——
#: 为未挂全局中间件的装配面兜底；关 = 直通（原行为）。
router = APIRouter(dependencies=[Depends(verify_api_key)])

#: event_type 长度上限（控制字符在下方显式拒绝）
MAX_EVENT_TYPE_LEN = 64
#: payload JSON 序列化后的字符上限
MAX_PAYLOAD_CHARS = 8192


class EventIn(BaseModel):
    event_type: str = PydField(min_length=1, max_length=MAX_EVENT_TYPE_LEN)
    payload: Dict[str, Any] = PydField(default_factory=dict)
    #: 外部系统唯一 ID（去重键；缺省不去重）——有界：它会进事件日志，
    #: 无界大键会绕过 payload 上限撑爆日志（与 payload 同理必须设界）
    event_key: Optional[str] = PydField(default=None, max_length=256)
    #: 业务侧时刻（源系统产生）；缺省用接收时刻
    occurred_at: Optional[float] = None
    #: 事件有效期（秒）；0 = 不过期
    ttl_s: float = PydField(default=0.0, ge=0.0, le=86400.0)


def _reject_control_chars(event_type: str) -> None:
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in event_type):
        raise HTTPException(status_code=422, detail="event_type contains control characters")


@router.post("", status_code=202)
async def publish_event(body: EventIn) -> Dict[str, Any]:
    """发布外部事件；返回 202 + event_id（去重命中时返回既有 id）。"""
    _reject_control_chars(body.event_type)
    try:
        raw = json.dumps(body.payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:  # pragma: no cover — pydantic 已限形状
        raise HTTPException(status_code=422, detail=f"payload not serializable: {exc}") from exc
    if len(raw) > MAX_PAYLOAD_CHARS:
        raise HTTPException(status_code=413, detail="event payload too large")

    from agentix.agent.events.port import get_event_port

    event_id = await get_event_port().publish(
        body.event_type,
        body.payload,
        event_key=body.event_key,
        occurred_at=body.occurred_at,
        ttl_s=body.ttl_s,
    )
    return {"event_id": event_id, "status": "accepted"}
