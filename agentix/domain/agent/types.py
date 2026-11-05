"""Agent Core 2.0 领域类型（Interface Freeze，Phase 1；M0 迁自 ``domain/agent_types.py``）。

零第三方依赖（仅 stdlib + typing）。与 ``application.services.task_plan.TaskSpec``
的关系：后者是**波次调度**用的子任务声明；本模块的 :class:`TaskSpec` 是
Agent Core 主循环的**开放任务**描述（goal / context 锚点 / 终止条件），
二者互补、不互相替代。

设计原则：
- Append-only：Observation / Plan 只追加，不原地改写；
- 可序列化：全部 dataclass 字段为纯数据，便于 trajectory / checkpoint。

M0（统一执行链）：本文件为**规范位**；旧路径（``agentix.domain.agent_types``）
迁移期为 re-export shim（类型对象同一），已于 M6 清尾删除。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Tuple

__all__ = [
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
]


class FailureLayer(str, Enum):
    """四层故障分类（书 §5.7 / domain.errors.ErrorCode 的运行时投影）。"""

    API = "api"                # 上游模型
    TOOL = "tool"              # 工具/检索/图库
    CONTEXT = "context"        # 上下文溢出/预算
    CONTROL_FLOW = "control"   # 步数/超时/控制流


class MemoryScope(str, Enum):
    """记忆两作用域（书 §3.1）：用户画像 vs 知识库/任务。"""

    USER = "user"
    KNOWLEDGE = "knowledge"
    TASK = "task"


class MemoryKind(str, Enum):
    """五类记忆（书 §3；Procedural 为 v1.1 新增）。"""

    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    USER = "user"


@dataclass(frozen=True)
class TaskSpec:
    """Agent Core 开放任务描述。

    :param goal: 自然语言目标（Planner 的唯一输入契约之一）。
    :param task_id: 稳定任务 ID（trajectory / 记忆回溯锚点）。
    :param constraints: 硬约束（预算、风险上限、必须遵守的规则）。
    :param success_criteria: 可机检的成功条件（Verifier 消费）。
    """

    goal: str
    task_id: str = ""
    constraints: Dict[str, Any] = field(default_factory=dict)
    success_criteria: Tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanStep:
    """计划中的单步（不可变；失败重规划产出**新** Plan，不改旧步）。"""

    id: str
    action: str
    depends_on: Tuple[str, ...] = ()
    tool: Optional[str] = None
    rationale: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Plan:
    """一次规划结果（Append-only：Replanner 产出新 Plan 替换引用）。"""

    steps: Tuple[PlanStep, ...] = ()
    version: int = 1
    planner: str = "default"

    def step_by_id(self, step_id: str) -> Optional[PlanStep]:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None


@dataclass(frozen=True)
class Observation:
    """一步执行的观测（工具结果 / LLM 输出 / 验证回执）。Append-only。"""

    step_id: str
    content: str
    ok: bool = True
    failure_layer: Optional[FailureLayer] = None
    error_code: Optional[str] = None
    artifacts: Tuple[str, ...] = ()
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VerificationResult:
    """Verifier 输出。Proposer–Reviewer：**不得**包含 Proposer 自述。"""

    passed: bool
    reason: str = ""
    score: Optional[float] = None
    failure_layer: Optional[FailureLayer] = None
    evidence_ids: Tuple[str, ...] = ()
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryItem:
    """统一记忆条目（三 scope × 内容；PII 脱敏在写入适配层完成）。"""

    content: str
    kind: MemoryKind = MemoryKind.EPISODIC
    scope: MemoryScope = MemoryScope.TASK
    item_id: str = ""
    subject: str = ""          # user_id / task_id / kb_id 等 scope 主体
    confidence: float = 1.0
    tags: Tuple[str, ...] = ()
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextSnapshot:
    """一次 Context 组装的不可变快照（Static Prefix + Trajectory 切片）。"""

    prompt: str
    token_estimate: int = 0
    truncated: bool = False
    status_bar: Dict[str, Any] = field(default_factory=dict)
    included_memory_ids: Tuple[str, ...] = ()
    included_evidence_ids: Tuple[str, ...] = ()
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def static_prefix(self) -> str:
        """稳定前缀：状态栏 + 系统约束（书 §2.4；Trajectory 在其后追加）。"""
        # 状态栏字段按 key 排序保证确定性
        if not self.status_bar:
            return ""
        parts = [f"{k}={self.status_bar[k]}" for k in sorted(self.status_bar)]
        return "\n".join(parts)
