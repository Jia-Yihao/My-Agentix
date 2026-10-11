"""Agent Core 2.0 七端口（Interface Freeze，Phase 1；M0 迁自 ``domain/ports/agent_core.py``）。

Harness 五职责 ↔ 端口对照（规划 §0）：

======== =============== ======================================
职责     端口            说明
======== =============== ======================================
Context  ContextPort     组装 Static Prefix + Trajectory
规划     PlannerPort     TaskSpec → Plan
工具     ToolSelectorPort 按策略选工具 schema / 能力
验证     VerifierPort    Observation → VerificationResult
纠偏     RollbackPort    检查点恢复 / 回滚
记忆     MemoryPort      读写五类记忆（scope 隔离）
事件     EventPort       外部事件唤醒（Long-running）
======== =============== ======================================

零第三方依赖；``@runtime_checkable`` 便于结构化子类型判定。
实现分别位于 agentix.agent.*（Phase 2+）与 application 桥接层。

Phase 22 增补：``CompactorPort``（L4 语义压缩，见下）——不改变上述
七端口的 Interface Freeze，仅作为 Context 家族的**可选**扩展端口。

M0（统一执行链）：本文件为**规范位**；旧路径 ``domain/ports/agent_core.py``
保留为 re-export shim（端口对象同一），M6 清尾。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, Sequence, runtime_checkable

from agentix.domain.agent.types import (
    ContextSnapshot,
    MemoryItem,
    Observation,
    Plan,
    TaskSpec,
    VerificationResult,
)

__all__ = [
    "PlannerPort",
    "VerifierPort",
    "ToolSelectorPort",
    "MemoryPort",
    "ContextPort",
    "CompactorPort",
    "EventPort",
    "RollbackPort",
]


@runtime_checkable
class PlannerPort(Protocol):
    """TaskSpec → Plan。实现须 budget-aware 且检测 DAG cycle。"""

    async def plan(self, task: TaskSpec, *, context: Optional[ContextSnapshot] = None) -> Plan:
        ...


@runtime_checkable
class VerifierPort(Protocol):
    """Observation → VerificationResult。

    Proposer–Reviewer：Verifier 只看产物/证据/结构化参数，
    **不得**读取 Proposer 的自然语言自述字段。
    """

    async def verify(
        self,
        task: TaskSpec,
        plan: Plan,
        observations: Sequence[Observation],
    ) -> VerificationResult:
        ...


@runtime_checkable
class ToolSelectorPort(Protocol):
    """按策略返回可暴露给模型的工具 schema 列表（纯数据）。"""

    async def select(
        self,
        task: TaskSpec,
        *,
        max_risk_level: str = "L1",
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        ...


@runtime_checkable
class MemoryPort(Protocol):
    """五类记忆读写；scope 隔离；读取时冲突裁决由实现负责。"""

    async def recall(
        self,
        *,
        scope: str,
        subject: str,
        query: str = "",
        limit: int = 10,
    ) -> List[MemoryItem]:
        ...

    async def remember(self, item: MemoryItem) -> str:
        """写入并返回 item_id（实现须做 PII 脱敏与去重）。"""
        ...


@runtime_checkable
class ContextPort(Protocol):
    """组装 ContextSnapshot；超预算 deterministic；状态栏不信任模型自写。"""

    async def build(
        self,
        task: TaskSpec,
        *,
        trajectory: Sequence[Observation] = (),
        budget_tokens: int = 0,
    ) -> ContextSnapshot:
        ...


@runtime_checkable
class CompactorPort(Protocol):
    """L4 语义压缩（Phase 22，Ch2 §2.7 分层压缩最后一档）。

    仅当 L1–L3 组装后仍超预算时作为**最后手段**调用（默认
    ``CONTEXT_AUTOCOMPACT=False`` 关闭）。实现契约：

    - 输入为待压缩正文与目标 token 预算，输出为压缩后正文；
    - 五要素（trace_id、错误记录、约束 key、来源引用、任务状态字段）
      必须原样保留——由**调用方**做机制断言校验，缺一即弃用回退
      （domain 只定协议，不绑定任何 LLM/网络）；
    - 实现可能失败/超时——调用方须兜底，压缩失败 ⇒ Phase 18 丢弃行为。
    """

    async def compact(self, text: str, *, budget_tokens: int) -> str:
        ...


@runtime_checkable
class EventPort(Protocol):
    """外部事件注册与唤醒（Phase 9 Event Adapter 的抽象面）。"""

    async def publish(
        self,
        event_type: str,
        payload: Dict[str, Any],
        *,
        event_key: Optional[str] = None,
        occurred_at: Optional[float] = None,
        ttl_s: float = 0.0,
        max_retries: int = 3,
    ) -> str:
        """发布事件，返回 event_id；event_key 幂等去重（webhook 依赖这三个 kwarg）。"""
        ...

    async def wait(
        self, event_type: str, *, timeout_s: float = 0.0, poll_s: float = 0.01
    ) -> Optional[Dict[str, Any]]:
        """等待事件；timeout_s<=0 表示非阻塞 poll。"""
        ...


@runtime_checkable
class RollbackPort(Protocol):
    """检查点快照与恢复（Memory ≠ Checkpoint：本端口管 Runtime 断点）。"""

    async def snapshot(self, run_id: str, state: Dict[str, Any]) -> str:
        """落盘/落库检查点，返回 checkpoint_id。"""
        ...

    async def restore(self, run_id: str, checkpoint_id: str) -> Dict[str, Any]:
        """恢复到指定检查点；不存在则抛异常。"""
        ...
