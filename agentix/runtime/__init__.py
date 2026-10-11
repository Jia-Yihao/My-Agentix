"""Agent Runtime（R3-1，依据《GustoBot 生产级 Agent 系统架构与实现设计》§7-11/§100）。

核心原则（文档 §2.1）：**LangGraph 是 Execution Backend，而不是系统架构本身。**

本包是 Agent Runtime 的领域层骨架，暂时包装既有 LangGraph 执行
（Runtime ↓ Existing LangGraph，文档 §100 Phase 1），不改变业务能力：

- ``state``       AgentStatus / AgentState —— 统一执行上下文（文档 §7/§8）
- ``steps``       ExecutionStep —— 步骤级轨迹领域模型（文档 §9）
- ``budget``      Budget / BudgetManager —— 防无限循环（文档 §11）
- ``lifecycle``   LifecycleManager —— 状态机合法转换（文档 §7）
- ``runtime``     AgentRuntime —— 包装执行、录步骤、控预算、管生命周期
- ``control_plane`` ControlPlane —— Run 登记 / 取消 / 准入 / 预算归属（M4 迁入，
  旧路径 ``application/services/control_plane.py`` 为 shim；仅子模块，未入本
  ``__init__`` 汇总导出——``from agentix.runtime.control_plane import ...`` 取用）

迁移策略（文档 §98）：Strangler Pattern——逐步替换，不做大爆炸重构。
"""

from agentix.domain.runtime.budget import (
    Budget,
    BudgetExceededError,
    BudgetState,
)
from agentix.runtime.budget import BudgetManager
from agentix.runtime.lifecycle import (
    InvalidTransitionError,
    LifecycleManager,
)
from agentix.runtime.runtime import AgentRuntime
from agentix.domain.runtime.steps import StepStatus
from agentix.runtime.state import (
    AgentState,
    AgentStatus,
    ErrorInfo,
    PolicyContext,
    Task,
)
from agentix.domain.runtime.steps import StepType
from agentix.runtime.steps import ExecutionStep

__all__ = [
    "AgentRuntime",
    "AgentState",
    "AgentStatus",
    "Budget",
    "BudgetExceededError",
    "BudgetManager",
    "BudgetState",
    "ErrorInfo",
    "ExecutionStep",
    "InvalidTransitionError",
    "LifecycleManager",
    "PolicyContext",
    "StepStatus",
    "StepType",
    "Task",
]
