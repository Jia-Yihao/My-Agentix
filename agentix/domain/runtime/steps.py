"""Step 契约（M0 拆入自 ``runtime/state.py`` StepStatus + ``runtime/steps.py`` StepType）。"""

from enum import Enum

__all__ = [
    "StepStatus",
    "StepType",
]


class StepStatus(str, Enum):
    """ExecutionStep 状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class StepType:
    """步骤类型常量（文档 §9 示例 + 检索域扩展）。"""

    ROUTE = "route"
    PLAN = "plan"
    TOOL = "tool"
    RETRIEVE = "retrieve"
    VERIFY = "verify"
    GENERATE = "generate"
    GUARDRAIL = "guardrail"
    EXECUTING = "executing"
