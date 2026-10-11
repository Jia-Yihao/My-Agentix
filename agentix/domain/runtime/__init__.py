"""Runtime 契约包（M0 拆入：Run / Step / Budget / Lifecycle 纯类型）。

零第三方依赖；实现留在 ``agentix/runtime/``（StepStatus 等旧位置为
re-export shim，M6 清尾）。新代码请从本包导入。
"""

from agentix.domain.runtime.budget import Budget, BudgetExceededError, BudgetState
from agentix.domain.runtime.lifecycle import ExitReason, ExitStatus
from agentix.domain.runtime.runs import TERMINAL_STATES, RunState
from agentix.domain.runtime.steps import StepStatus, StepType

__all__ = [
    # Run
    "RunState",
    "TERMINAL_STATES",
    # Step
    "StepStatus",
    "StepType",
    # Budget
    "BudgetState",
    "Budget",
    "BudgetExceededError",
    # Lifecycle
    "ExitStatus",
    "ExitReason",
]
