"""Agent Core 契约包（M0 拆分自 ``domain/agent_types.py`` + ``domain/ports/agent_core.py``）。

- ``types``  —— Task / Plan / Observation / Verification / Memory / Context 快照
  （frozen dataclass，零第三方依赖）；
- ``ports``  —— 七端口 + CompactorPort（``typing.Protocol``，runtime_checkable）。

旧路径 ``agentix.domain.agent_types`` 与 ``agentix.domain.ports.agent_core``
为 re-export shim（对象同一），M6 清尾；新代码请从本包导入。
"""

from agentix.domain.agent.ports import (
    CompactorPort,
    ContextPort,
    EventPort,
    MemoryPort,
    PlannerPort,
    RollbackPort,
    ToolSelectorPort,
    VerifierPort,
)
from agentix.domain.agent.types import (
    ContextSnapshot,
    FailureLayer,
    MemoryItem,
    MemoryKind,
    MemoryScope,
    Observation,
    Plan,
    PlanStep,
    TaskSpec,
    VerificationResult,
)

__all__ = [
    # types
    "TaskSpec",
    "Plan",
    "PlanStep",
    "Observation",
    "VerificationResult",
    "MemoryItem",
    "MemoryScope",
    "MemoryKind",
    "ContextSnapshot",
    "FailureLayer",
    # ports
    "PlannerPort",
    "VerifierPort",
    "ToolSelectorPort",
    "MemoryPort",
    "ContextPort",
    "CompactorPort",
    "EventPort",
    "RollbackPort",
]
