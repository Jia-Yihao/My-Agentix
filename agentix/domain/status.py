"""请求状态与退出契约枚举（P2-2 收敛 ``status``；A1b 补退出契约）。

历史问题：``chat_service`` 里 ``status`` 直接写裸字符串字面量
（``"ok"`` / ``"degraded"`` / ``"error"``），拼错也不会报错。用 ``str`` 枚举
把它收敛成受约束的取值集合。

A1b（统一执行链）：新增 :class:`ExitStatus` / :class:`ExitReason`——
「一次运行/任务怎么结束的」的**统一退出契约**，由 ``LoopResult``（属性）与
``TaskOutcome``（字段）共同实现，替代各处散落的 terminal/ok 裸字符串推断。

放在 ``domain/status.py``（而非 ``domain/errors.py``）：errors 模块由另一条
并行工作流编辑，避免冲突。``str, Enum`` 混入保证 ``RequestStatus.OK == "ok"``
与 ``ExitStatus.COMPLETED == "completed"``（JSON 契约保持普通字符串）——
调用点用 ``.value`` 落地。

M0（统一执行链）：``ExitStatus`` / ``ExitReason`` 规范位已迁
``domain/runtime/lifecycle.py``；M6 清尾后本模块**不再** re-export
（调用方一律从规范位导入）；``RequestStatus``（HTTP 响应契约）留在本模块。
"""

from enum import Enum

__all__ = [
    "RequestStatus",
]


class RequestStatus(str, Enum):
    """单次请求的结果状态（与 HTTP 响应体 ``status`` 字段一一对应）。"""

    OK = "ok"
    DEGRADED = "degraded"
    ERROR = "error"
