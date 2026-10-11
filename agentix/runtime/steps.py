"""ExecutionStep——步骤级轨迹领域模型（文档 §9）。

每次 Agent 运行产生一条步骤序列（Agent Trajectory）::

    Step 1  type=ROUTE    name=intent_router
    Step 2  type=PLAN     name=legal_research_planner
    Step 3  type=TOOL     name=legal_search
    Step 4  type=TOOL     name=neo4j_query
    Step 5  type=VERIFY   name=citation_validator
    Step 6  type=GENERATE name=final_answer

与 ``observability/trajectory.py`` 的关系：trajectory 是**观测层**的 dict
采集（span 驱动），ExecutionStep 是**领域层**的类型化模型；AgentRuntime
在步骤完成时 best-effort 镜像到 trajectory recorder，二者不互相依赖。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# M0 清尾（M6）：StepStatus 规范位在 domain/runtime/steps.py，本模块直连规范位。
from agentix.domain.runtime.steps import StepStatus


@dataclass
class ExecutionStep:
    """单步执行记录（字段对齐文档 §9）。"""

    step_type: str
    name: str
    step_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    input: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] | None = None
    status: StepStatus = StepStatus.PENDING
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    latency_ms: int | None = None
    retry_count: int = 0
    tool_call_id: str | None = None
    trace_id: str | None = None
    error: str | None = None
    _t0: float = field(default_factory=time.perf_counter, repr=False)

    def start(self) -> None:
        """标记开始（幂等：重复调用刷新计时起点）。"""
        self.status = StepStatus.RUNNING
        self.started_at = datetime.now(timezone.utc)
        self._t0 = time.perf_counter()

    def complete(self, output: dict[str, Any] | None = None) -> None:
        """标记成功并结算耗时。"""
        self.status = StepStatus.SUCCEEDED
        self.output = output or {}
        self._finalize()

    def fail(self, error: str | None = None) -> None:
        """标记失败并结算耗时。"""
        self.status = StepStatus.FAILED
        self.error = error
        self._finalize()

    def skip(self, reason: str | None = None) -> None:
        """标记跳过（预算超限/权限不足等）。"""
        self.status = StepStatus.SKIPPED
        self.error = reason
        self._finalize()

    def _finalize(self) -> None:
        self.completed_at = datetime.now(timezone.utc)
        self.latency_ms = int((time.perf_counter() - self._t0) * 1000)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "step_type": self.step_type,
            "name": self.name,
            "status": self.status.value,
            "input": self.input,
            "output": self.output,
            "latency_ms": self.latency_ms,
            "retry_count": self.retry_count,
            "tool_call_id": self.tool_call_id,
            "trace_id": self.trace_id,
            "error": self.error,
        }
