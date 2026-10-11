"""检索域策略层：纯函数决策逻辑（不含 I/O）。"""

from agentix.domain.policies.legal_effectiveness import (
    STATUS_EXPIRED,
    STATUS_IN_FORCE,
    STATUS_PENDING,
    STATUS_REPEALED,
    append_retrieved_asof,
    attach_effective_status,
    effective_status_of,
    filter_in_force,
    format_retrieved_asof,
    is_in_force,
)

__all__ = [
    "STATUS_EXPIRED",
    "STATUS_IN_FORCE",
    "STATUS_PENDING",
    "STATUS_REPEALED",
    "append_retrieved_asof",
    "attach_effective_status",
    "effective_status_of",
    "filter_in_force",
    "format_retrieved_asof",
    "is_in_force",
]
