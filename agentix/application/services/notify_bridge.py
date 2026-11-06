"""``notify_user`` 组合根装配（Phase 23）。

职责切分（声明 / 校验 / 接线三段）：

- **声明**（spec + LLM schema）在 ``capabilities/tools/registry_init.py``，
  由 ``settings.NOTIFY_USER_ENABLED`` gate（flag-off 不进声明清单）；
- **纯参数校验**在 ``capabilities/tools/notify_user.py``；
- 本模块只做接线：``ensure_registered()`` + ``register_handler``——
  handler 把通知落 ``EventPort``（type=``user_notification``），
  ``main.py::_startup`` 仅在 flag-on 时调用。

出站即副作用：handler 经 ``ToolExecutor.execute`` 执行 ⇒ 有效风险
（声明 L1 ∪ 词表 notify → MEDIUM）/ 权限 / HITL 审批闸门全部在前，
本模块不提供任何绕过路径（Gotcha 1）。
"""

from __future__ import annotations

from agentix.config.settings import settings

__all__ = ["install_notify_user_tool"]


def install_notify_user_tool() -> None:
    """注册 ``notify_user`` 处理器（幂等；依赖 flag-on 声明清单已有 spec）。"""
    if not settings.NOTIFY_USER_ENABLED:
        return  # belt-and-suspenders：调用方已判 flag，这里再判一次
    from agentix.tools.gateway.executor import get_tool_executor
    from agentix.tools.adapters.notify_user import (
    NOTIFY_USER_EVENT_TYPE,
    validate_notify_args,
)
    from agentix.tools.registry_init import ensure_registered

    ensure_registered()  # spec 先入 registry（executor 执行点要求 spec 存在）

    async def _handler(args: dict) -> dict:
        from agentix.agent.events.port import get_event_port
        from agentix.domain.tracing import get_trace_id

        normalized = validate_notify_args(args)  # 非法输入抛 ValueError → ToolResult 失败
        payload = {
            **normalized,
            "trace_id": get_trace_id(),
        }
        event_id = await get_event_port().publish(
            NOTIFY_USER_EVENT_TYPE, payload
        )
        return {
            "status": "queued",
            "event_type": NOTIFY_USER_EVENT_TYPE,
            "event_id": event_id,
            **normalized,
        }

    get_tool_executor().register_handler("notify_user", _handler)
