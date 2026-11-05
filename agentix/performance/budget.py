"""延迟总预算。

语义：

- ``LATENCY_TOTAL_BUDGET_MS = 0``（默认）= 不限额，恒返回 None，零行为变化；
- ``> 0`` 时对**已完成**的总耗时给出 ok/over 判词；over 仅告警 + 打点
  ``record_degraded("latency_budget")``（fail-open：**绝不中断响应、绝不重试**）。

预算的职责是「让超时可见」，不是「让超时失败」——裁决交给 SLO/告警系统。
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_OVER_WARNED = False


def budget_ms() -> int:
    """当前生效的延迟总预算（毫秒）；<=0 表示不限额。"""
    try:
        from agentix.config import settings

        return int(getattr(settings, "LATENCY_TOTAL_BUDGET_MS", 0) or 0)
    except Exception:  # noqa: BLE001 - 预算读取失败按不限额处理，绝不影响主链路
        return 0


def evaluate(total_ms: float) -> Optional[str]:
    """判词：None=不限额；"ok"=预算内；"over"=超预算。"""
    budget = budget_ms()
    if budget <= 0:
        return None
    return "ok" if total_ms <= budget else "over"


def enforce(total_ms: float, *, source: str = "stream") -> Optional[str]:
    """评估并（在超限时）留痕：warning 日志 + degraded 打点。fail-open。"""
    verdict = evaluate(total_ms)
    if verdict != "over":
        return verdict
    global _OVER_WARNED
    try:
        if not _OVER_WARNED:
            # stdlib logging 是 %-风格插值（loguru 才是 {}）——写错会被 except 吞掉
            logger.warning(
                "latency budget exceeded: source=%s total_ms=%s budget_ms=%s",
                source,
                int(total_ms),
                budget_ms(),
            )
            _OVER_WARNED = True  # 每进程只刷一次屏，避免日志风暴
        from agentix.observability.metrics import record_degraded

        record_degraded("latency_budget")
    except Exception:  # noqa: BLE001 - 留痕失败不影响主链路
        pass
    return verdict
