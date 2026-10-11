"""F1d·A-P1-3（并发工具轮）派发前闸：授权门 + 危险判定（L2/L3 两面共用）。

- **授权门**：flag-on（``AGENT_TOOL_ROUND_CONCURRENCY>1``）并发批在 gather
  派发前**逐调用**同步 preflight——沿 ToolExecutor ``_review_preflight`` 的
  执行点语义（``consume=True``，一次批准精确对应一次执行）；HUMAN_REVIEW
  关闭时闸门第一行即返回 ⇒ 零副作用恒放行；启用且未获批 ⇒ 返回阻断消息
  （fail-closed，消费方落 error 结果、不派发）。
- **危险判定**：有效风险（名称模式 + 声明级 + 参数提权取 max）∈
  ``REVIEW_RISK_LEVELS``（MEDIUM+ = A-P1-4 语义的 L1+）⇒ 危险工具强制
  串行（永不与同批任何调用并发）。
- 消费方：L2 ``chat_agent_runner._execute_tool_calls`` 与 L3 ``AgentLoop``
  批路径（两处均**惰性 import**——retrieval→tools 与 orchestration→tools
  的新边按 ``orchestration/executor.py`` result_limiter 先例走函数内 import）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

__all__ = ["is_dangerous", "preflight_block"]


def is_dangerous(tool_name: str, args: Optional[Dict[str, Any]] = None) -> bool:
    """危险判定：有效风险 ∈ ``REVIEW_RISK_LEVELS``（MEDIUM+）即危险。

    判定故障 fail-open 为**危险**（保守串行，绝不因判定坏了就放开并发）。
    """
    if not tool_name:
        return False
    try:
        from agentix.tools.gateway.human_review import (
            REVIEW_RISK_LEVELS,
            effective_risk_level,
        )

        level = effective_risk_level(tool_name, args)
    except Exception:  # noqa: BLE001 - 判定故障 ⇒ 保守按危险串行
        return True
    return level.value in REVIEW_RISK_LEVELS


def preflight_block(tool_name: str, args: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """执行点授权门：``None`` = 放行；非空 = fail-closed 阻断消息。

    与 ``ToolExecutor._review_preflight`` 同款语义：闸门不可用/故障一律
    fail-closed（绝不因闸坏了放行高风险调用）。
    """
    if not tool_name:
        return None
    from agentix.tools.gateway.human_review import ReviewRequired, preflight_review

    try:
        preflight_review(tool_name, args, consume=True)
    except ReviewRequired as exc:
        request = exc.request
        level = getattr(getattr(request, "risk_level", None), "value", "unknown")
        return f"human review required: {tool_name} ({level})"
    except Exception as exc:  # noqa: BLE001 - 闸门故障 fail-closed
        return f"review preflight failed (fail-closed): {exc}"
    return None
