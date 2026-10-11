"""Lifecycle 退出契约（M0 拆入自 ``domain/status.py`` ExitStatus / ExitReason）。

「一次运行/任务怎么结束的」的统一退出契约，由 ``LoopResult``（属性）与
``TaskOutcome``（字段）共同实现，替代各处散落的 terminal/ok 裸字符串推断。
``str, Enum`` 混入保证 ``ExitStatus.COMPLETED == "completed"``（JSON 契约
保持普通字符串）——调用方用 ``.value`` 落地。
"""

from enum import Enum

__all__ = [
    "ExitStatus",
    "ExitReason",
]


class ExitStatus(str, Enum):
    """退出状态（统一退出契约；LoopResult / TaskOutcome 共用）。

    - ``COMPLETED``：正常完成（不论验证结论如何，流程走完）；
    - ``INTERRUPTED``：被外部打断（取消 / 客户端断连）——失败与打断
      分开表达，调用方不该把「用户取消」当「执行出错」呈现；
    - ``FAILED``：执行失败（含预算耗尽与验证不通过，细分见 ``ExitReason``）。
    """

    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class ExitReason(str, Enum):
    """退出原因（在 ExitStatus 之上的细分，供轨迹/Run API 呈现）。"""

    COMPLETED = "completed"
    #: 步数/重试预算耗尽（LoopResult.terminal=budget_exceeded；重试耗尽）
    MAX_ITERATIONS = "max_iterations"
    INTERRUPTED = "interrupted"
    #: 一般执行失败（异常、验证不通过、非可重试错误）
    ERROR = "error"
