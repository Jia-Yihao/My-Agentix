"""领域层抽象接口（ports）——application 依赖本包，infrastructure 实现本包。

依据方案 2.2 节依赖规则 3：``domain`` **不得**依赖任何 ``infrastructure`` /
``interfaces`` 符号。本包因此保持零第三方依赖（仅 stdlib + typing）。

Agent Core 2.0（Phase 1）：七端口规范位在 :mod:`agentix.domain.agent.ports`
（M6 清尾删除 ``agent_core`` shim 后，本包自规范位再导出以保 ``domain.ports`` 历史面）。
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
from .graph import GraphProviderPort
from .payloads import (
    ConversationSnapshotPayload,
    MessageCreatePayload,
    SessionCreatePayload,
    SessionSnapshotPayload,
)
from .repository import MessageRepositoryPort, SessionRepositoryPort
from .retrieval import (
    IndexStatus,
    RetrievalChunk,
    RetrievalCitation,
    RetrievalPort,
    RetrievalResult,
)

__all__ = [
    "GraphProviderPort",
    "MessageRepositoryPort",
    "SessionRepositoryPort",
    "SessionCreatePayload",
    "MessageCreatePayload",
    "SessionSnapshotPayload",
    "ConversationSnapshotPayload",
    "RetrievalPort",
    "RetrievalResult",
    "RetrievalChunk",
    "RetrievalCitation",
    "IndexStatus",
    "PlannerPort",
    "VerifierPort",
    "ToolSelectorPort",
    "MemoryPort",
    "ContextPort",
    "CompactorPort",
    "EventPort",
    "RollbackPort",
]
