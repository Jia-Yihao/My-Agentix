"""``notify_user`` 出站通知工具声明与参数校验（Phase 23，Ch6 §6.2）。

- **声明**（spec + LLM schema）在本文件，由 ``registry_init`` 在
  ``NOTIFY_USER_ENABLED=true`` 时挂进声明清单（flag-off 零注册）；
- **实现**（handler → EventPort.publish）在组合根
  ``application/services/notify_bridge.py``（capabilities 不反向依赖
  agent/application——与 Phase 21 hooks 同款职责切分）；
- 本批默认实现 = 落 EventPort（type=``user_notification``）+ ops 可查，
  **不接真实邮件/短信**（通道后续逐个挂）。

出站即副作用：工具名命中 ``human_review.py`` 词表（notify → MEDIUM）+
声明级 L1 → MEDIUM ⇒ 走既有 HITL 闸门，不得绕过 ``ToolExecutor``。
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel, Field

from agentix.tools.definitions import ToolSpec

__all__ = [
    "NotifyUserSchema",
    "NOTIFY_USER_EVENT_TYPE",
    "NOTIFY_PRIORITIES",
    "MAX_MESSAGE_CHARS",
    "validate_notify_args",
    "build_notify_user_spec",
]

#: 落入 EventPort 的事件类型（ops 按此过滤可查）。
NOTIFY_USER_EVENT_TYPE = "user_notification"

#: 允许的优先级（未知值 fail-closed 拒绝，不静默降级）。
NOTIFY_PRIORITIES = ("low", "normal", "high", "urgent")

#: 正文上限（事件日志有界；绕过上限的大消息在声明期挡不住，执行期拒绝）。
MAX_MESSAGE_CHARS = 4000


class NotifyUserSchema(BaseModel):
    """``notify_user`` 入参（仅 ``NOTIFY_USER_ENABLED=true`` 进声明清单）。"""

    channel: str = Field(
        description="通知通道标识（如 in_app/email/sms/webhook——本批仅记录落事件，不投递真实通道）"
    )
    message: str = Field(description="面向用户的通知正文（必填，有界）")
    priority: str = Field(
        default="normal",
        description=f"优先级：{'/'.join(NOTIFY_PRIORITIES)}（默认 normal）",
    )


def _strip_control(text: str) -> str:
    """剥离 C0/C1 控制字符（保留换行/制表）——通知正文会进日志/前端展示。"""
    out = []
    for ch in text:
        if ch in ("\n", "\t"):
            out.append(ch)
        elif 32 <= ord(ch) < 127 or ord(ch) >= 160:
            out.append(ch)
    return "".join(out)


def validate_notify_args(args: Dict[str, Any]) -> Dict[str, str]:
    """校验并规范化 ``notify_user`` 入参；非法输入抛 ``ValueError``（fail-fast）。

    返回可直接进 EventPort payload 的规范化字典（channel/message/priority）。
    """
    channel = _strip_control(str(args.get("channel") or "")).strip()
    message = _strip_control(str(args.get("message") or "")).strip()
    priority = str(args.get("priority") or "normal").strip().lower()

    if not channel or len(channel) > 64:
        raise ValueError("channel must be a non-empty string (<=64 chars)")
    if not message:
        raise ValueError("message must be a non-empty string")
    if len(message) > MAX_MESSAGE_CHARS:
        raise ValueError(f"message exceeds {MAX_MESSAGE_CHARS} chars")
    if priority not in NOTIFY_PRIORITIES:
        raise ValueError(
            f"priority must be one of {NOTIFY_PRIORITIES}, got {priority!r}"
        )
    return {"channel": channel, "message": message, "priority": priority}


def build_notify_user_spec() -> ToolSpec:
    """构造 ``notify_user`` 声明（L1 → 审批级 MEDIUM，出站走 HITL 闸）。"""
    return ToolSpec(
        name="notify_user",
        risk_level="L1",  # L1 → MEDIUM（_L_TO_RISK 唯一映射）；词表 notify → 亦 MEDIUM
        description=(
            "向用户发送一条出站通知（记录为 user_notification 事件，供 ops 查询）。\n"
            "何时用：任务产生需要用户在**非同步会话中**知晓的结果/待办/状态变化"
            "（如长任务完成、需要人工跟进的事项）。\n"
            "何时不能用：结论直接在当前对话回答即可（普通问答无需出站通知）；"
            "或仅需内部轨迹留痕（用普通回答，不要重复通知）。\n"
            "注意：本批仅落事件记录，不投递真实邮件/短信；出站即副作用，"
            "MEDIUM 风险——开启 HUMAN_REVIEW_ENABLED 时须人工批准后才执行。"
        ),
        parameters=NotifyUserSchema.model_json_schema(),
        side_effect=True,  # 出站即动作：不可按只读对待（Gotcha 1）
        capabilities=("interaction",),
        timeout_ms=10_000,
    )
