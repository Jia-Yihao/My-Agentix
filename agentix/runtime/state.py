"""AgentState 与生命周期状态（文档 §7/§8）。

AgentState 是 Agent Runtime 的**统一执行上下文**：不再让每个 LangGraph
子图各自定义完全不同的 State，而是收敛到领域层一个对象。

务实取舍（Strangler 迁移期）：
- ``messages`` / ``observations`` / ``artifacts`` / ``memory_refs`` /
  ``tool_calls`` / ``evidence`` 在 P0 阶段为 ``list[dict]``——与既有
  LangGraph State / trajectory 的 dict 形态互通，避免一次性定义全套
  领域模型（文档 §98：不要同时重构所有东西）；
- ``plan`` / ``task`` / ``budget`` / ``policy`` / ``error`` 为本包定义的
  轻量 dataclass（结构对齐文档 §8/§11/§13/§14）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

# M0 清尾（M6）：StepStatus / BudgetState 规范位在 domain/runtime/，调用方直连；
# 本模块仅保留自身字段需要的 BudgetState。
from agentix.domain.runtime.budget import BudgetState

if TYPE_CHECKING:
    # 仅为类型标注服务。必须放在 TYPE_CHECKING 内：``steps.py`` 反向依赖本模块，
    # 模块级导入会形成 state ↔ steps 循环导入。
    #
    # 原先 ``steps: list["ExecutionStep"]`` 直接引用了本模块并不存在的名字。
    # 在 ``from __future__ import annotations`` 掩护下运行时无感，但：
    #   1) 对该 dataclass 调用 ``typing.get_type_hints()``（pydantic/序列化框架
    #      常这么做）会立刻 NameError；
    #   2) ruff F821 报未定义名——CI 的 lint job 恰好扫 ``agentix``，会直接判失败。
    from agentix.runtime.steps import ExecutionStep


class AgentStatus(str, Enum):
    """Agent 运行生命周期（文档 §7 + Phase 8/9 扩展）。

    正常流：CREATED → RUNNING → PLANNING → EXECUTING → VERIFYING → COMPLETED
    异常：任意活动态 → FAILED
    审批：EXECUTING/RUNNING → WAITING_APPROVAL → RUNNING（PAUSED_FOR_APPROVAL 死转移已移除，A4）
    长任务：EXECUTING → PAUSED / WAITING_EXTERNAL_EVENT → RUNNING/EXECUTING
    取消：活动态 → CANCELLED
    """

    CREATED = "created"
    RUNNING = "running"
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # Phase 8 — HITL Sidecar
    WAITING_APPROVAL = "waiting_approval"
    # Phase 9 — Long-running Event Adapter
    PAUSED = "paused"
    WAITING_EXTERNAL_EVENT = "waiting_external_event"


@dataclass
class Task:
    """一次 Agent 运行的任务描述（文档 §8 ``task``）。"""

    kind: str = "chat"  # chat / legal_research / kb_qa / graph / sql / ...
    question: str = ""
    route_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ErrorInfo:
    """结构化错误（文档 §8 ``error``）。"""

    code: str
    message: str
    stage: str | None = None  # 失败发生阶段（route/plan/tool/verify/generate）
    retryable: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class PolicyContext:
    """策略上下文（文档 §8 ``policy``；P0 仅承载只读判定所需的最小集）。"""

    tenant_id: str | None = None
    user_role: str | None = None
    permissions: frozenset[str] = frozenset()
    max_risk_level: str = "L1"  # 允许执行的最高工具风险等级
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentState:
    """Agent Runtime 的统一执行上下文（文档 §8）。"""

    run_id: str
    session_id: str = ""
    user_id: str = ""
    task: Task = field(default_factory=Task)
    messages: list[dict[str, Any]] = field(default_factory=list)
    plan: dict[str, Any] | None = None
    steps: list[ExecutionStep] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    memory_refs: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    budget: BudgetState = field(default_factory=BudgetState)
    policy: PolicyContext = field(default_factory=PolicyContext)
    status: AgentStatus = AgentStatus.CREATED
    error: ErrorInfo | None = None

    def to_dict(self) -> dict[str, Any]:
        """可序列化快照（供 trajectory / eval / 持久化复用）。"""
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "task": {
                "kind": self.task.kind,
                "question": self.task.question,
                "route_type": self.task.route_type,
                "metadata": self.task.metadata,
            },
            "plan": self.plan,
            "steps": [s.to_dict() for s in self.steps],
            "status": self.status.value,
            "error": (
                {"code": self.error.code, "message": self.error.message, "stage": self.error.stage}
                if self.error
                else None
            ),
            "budget": {
                "max_steps": self.budget.max_steps,
                "max_llm_calls": self.budget.max_llm_calls,
                "max_tool_calls": self.budget.max_tool_calls,
                "max_tokens": self.budget.max_tokens,
                "max_latency_ms": self.budget.max_latency_ms,
                "max_cost_usd": self.budget.max_cost_usd,
                "used_steps": self.budget.used_steps,
                "used_llm_calls": self.budget.used_llm_calls,
                "used_tool_calls": self.budget.used_tool_calls,
                "used_tokens": self.budget.used_tokens,
                "elapsed_ms": self.budget.elapsed_ms,
            },
        }
