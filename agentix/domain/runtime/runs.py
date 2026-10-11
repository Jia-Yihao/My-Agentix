"""Run 契约（M0 拆入自 ``application/services/control_plane.py``）。

Run 实例状态与终态判定——control plane 的登记/准入实现留在
application 层，纯契约下沉 domain（零第三方依赖）。
"""

from enum import Enum

__all__ = [
    "RunState",
    "TERMINAL_STATES",
]

#: 终态集合
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})


class RunState(str, Enum):
    """Agent 运行实例的状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self.value in TERMINAL_STATES
