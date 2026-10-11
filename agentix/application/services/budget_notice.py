"""Phase 28（Ch10 §10.6 预算意识）：剩余额步只读可见——轨迹 append ``budget_notice``。

- **只写 append-only 轨迹**（kind="notice"，同 config_watch 范式），绝不进状态栏：
  状态栏位于静态前缀，动态剩余量每步换前缀 = 打穿 KV Cache（Phase 22 原则 3）；
- flag（``BUDGET_NOTICE_ENABLED``，默认关）内层门 = 零写入；构造侧另有外层门
  （adapter 只在 flag 开时注入 observer）——双层门，flag 关零成本；
- 全程自吞异常：观测失败绝不影响主链路；
- **双预算体系各插一处、防双写**：AgentLoop ``max_steps``（loop.py 经
  observer 回调）与图主循环 ``recursion_limit``（chat_service 流式事件按
  ``langgraph_step``）互斥生效——任何单次请求至多一条 notice。
  非流式 ``graph.ainvoke`` 的步数由 LangGraph 内部强制、无观测点 ⇒ 不插。
"""

from __future__ import annotations


from loguru import logger

from agentix.config.settings import settings

__all__ = ["budget_in_notice_zone", "record_budget_notice", "budget_notice_observer"]


def budget_in_notice_zone(*, step: int, limit: int) -> bool:
    """剩余步数（``limit - step``）是否进入 ≤2 提醒区（纯函数、可测）。"""
    if limit <= 0:
        return False
    remaining = limit - step
    return 0 <= remaining <= 2


def record_budget_notice(*, steps_used: int, max_steps: int) -> bool:
    """flag 开且在区内 ⇒ 轨迹 append ``budget_notice``；否则零写入。

    :return: 本次是否实际写入（供流式路径一次性去重）。
    """
    try:
        if not settings.BUDGET_NOTICE_ENABLED:
            return False
        if not budget_in_notice_zone(step=steps_used, limit=max_steps):
            return False
        from agentix.application.services.trajectory import get_trajectory_recorder

        get_trajectory_recorder().record(
            kind="notice",
            name="budget_notice",
            status="ok",
            detail={
                "steps_used": steps_used,
                "max_steps": max_steps,
                "remaining": max_steps - steps_used,
            },
        )
        return True
    except Exception:  # noqa: BLE001 - 可观测性不得影响主链路
        logger.debug("budget_notice record skipped (non-fatal)")
        return False


def budget_notice_observer(max_steps: int, steps_used: int) -> None:
    """AgentLoop ``budget_observer`` 入口（loop.py 仅在区内按步回调）。"""
    record_budget_notice(steps_used=steps_used, max_steps=max_steps)
