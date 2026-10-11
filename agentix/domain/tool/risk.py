"""HITL 风险等级契约（M0 拆入自 ``capabilities/tools/human_review.py`` 纯枚举）。"""

from enum import Enum

__all__ = [
    "RiskLevel",
]


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
