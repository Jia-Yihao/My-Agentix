"""控制平面：Agent 运行的 spawn / status / cancel / 预算。

《My Agentix_架构优化方案》§9.1 D 组缺口：「控制平面具备 spawn/status/cancel/预算分配」。
现状：``status`` ✅（``task_queue.TaskStatus``）、准入 ✅（``chat_service`` 信号量），
但 ``spawn`` / ``cancel`` / per-agent 预算 ❌。

**与 ``task_queue`` 的边界**（重要，避免职责重叠）：

=================  ==============================  ==========================
模块               管什么                           粒度
=================  ==============================  ==========================
``task_queue``     后台**作业**（file / embed / summary）  作业队列，可持久化
``control_plane``  **Agent 图运行**（本进程内）      运行实例，生命周期短
=================  ==============================  ==========================

控制平面管的是「一次 Agent 子图/子任务的运行实例」：给它一个 ``run_id``、
一份 :class:`TokenBudget` 切片，能在运行中被 ``cancel``，结束后留下终态与原因。
它**不做调度**（不决定先跑谁），只做**运行登记 + 取消 + 预算归属**——
这正是「控制平面」与「调度器」的分工。

**有界**：运行记录按条数封顶（FIFO 淘汰已终态的旧记录），防止长跑进程内存泄漏。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from loguru import logger

if TYPE_CHECKING:
    # 注解专用：``AgentRun.budget``/``spawn(budget=)`` 两处皆字符串注解
    # （``from __future__ import annotations``），运行期零引用——``to_dict()``
    # 走鸭子类型。保持 Runtime 簇**运行期自包含**，不产生 runtime → application
    # 运行期边（先例：``runtime/state.py`` 对 ``steps`` 的 TYPE_CHECKING 回指）。
    from agentix.application.services.token_budget import TokenBudget

__all__ = [
    "RunState",
    "AgentRun",
    "AdmissionDenied",
    "ControlPlane",
    "get_control_plane",
    "reset_control_plane",
]

#: 终态集合与 RunState —— M0 shim：规范位已迁 domain/runtime/runs.py
#: （对象同一，M6 清尾）
from agentix.domain.runtime.runs import TERMINAL_STATES, RunState  # noqa: F401


class AdmissionDenied(RuntimeError):
    """控制平面准入拒绝（并发/深度超限；Phase 25 Ch10 §10.17 防线）。

    **fail-closed**：调用方不得静默绕过（登记失败降级 ≠ 策略拒绝降级）——
    超限就是不放行，否则防线形同虚设。
    """


@dataclass
class AgentRun:
    """一次 Agent 运行实例的登记项。"""

    run_id: str
    agent_name: str
    state: RunState = RunState.PENDING
    trace_id: str = "-"
    budget: Optional[TokenBudget] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    error: Optional[str] = None
    cancel_reason: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    #: 关联的 asyncio.Task（用于真正打断）；不参与序列化
    _task: Optional["asyncio.Task"] = field(default=None, repr=False, compare=False)

    @property
    def duration_ms(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.ended_at if self.ended_at is not None else time.time()
        return max(0.0, (end - self.started_at) * 1000.0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent_name": self.agent_name,
            "state": self.state.value,
            "trace_id": self.trace_id,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": round(self.duration_ms, 2),
            "error": self.error,
            "cancel_reason": self.cancel_reason,
            "budget": self.budget.to_dict() if self.budget else None,
            "meta": self.meta,
        }


class ControlPlane:
    """进程内 Agent 运行登记表 + 取消 + 预算归属 + 准入。"""

    def __init__(
        self,
        *,
        max_runs: int = 500,
        max_concurrency: int = 0,
        max_depth: int = 0,
    ) -> None:
        self._runs: Dict[str, AgentRun] = {}
        self._order: List[str] = []
        self._max_runs = max(1, int(max_runs))
        #: 并发准入上限：``0`` = 不限（默认 = 接线前行为）
        self._max_concurrency = max(0, int(max_concurrency))
        #: 深度准入上限：``0`` = 不限
        self._max_depth = max(0, int(max_depth))

    # -- 准入（Phase 25） -----------------------------------------------------

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    @property
    def max_depth(self) -> int:
        return self._max_depth

    def check_admission(self, *, depth: int = 0) -> None:
        """准入检查：超限抛 :class:`AdmissionDenied`（fail-closed）。

        ``max_concurrency``/``max_depth`` 为 0 时对应维度不限——默认配置下
        永不拒绝，与接线前行为逐位等价。
        """
        if self._max_depth > 0 and int(depth) > self._max_depth:
            raise AdmissionDenied(
                f"admission denied: depth {depth} > max_depth {self._max_depth}"
            )
        if self._max_concurrency > 0 and self.active_count() >= self._max_concurrency:
            raise AdmissionDenied(
                f"admission denied: active runs {self.active_count()} "
                f">= max_concurrency {self._max_concurrency}"
            )

    # -- spawn ---------------------------------------------------------------

    def spawn(
        self,
        agent_name: str,
        *,
        run_id: Optional[str] = None,
        budget: Optional[TokenBudget] = None,
        trace_id: str = "-",
        meta: Optional[Dict[str, Any]] = None,
        depth: int = 0,
    ) -> AgentRun:
        """登记一次 Agent 运行，返回 ``run_id`` 载体。

        准入超限抛 :class:`AdmissionDenied`（不登记、不执行）。

        ``run_id``：可选的外部指定运行 ID（请求级统一 Run/Event 源等场景）；
        缺省自生成 ``uuid4().hex``。与既有记录重复时抛 ``ValueError``
        （不静默覆盖——同 ID 双登记会让 cancel 打错对象）。
        """
        self.check_admission(depth=depth)
        rid = str(run_id) if run_id else uuid.uuid4().hex
        if rid in self._runs:
            raise ValueError(f"run_id already registered: {rid}")
        run_meta = dict(meta or {})
        run_meta.setdefault("depth", int(depth))
        run = AgentRun(
            run_id=rid,
            agent_name=str(agent_name),
            state=RunState.PENDING,
            trace_id=trace_id,
            budget=budget,
            meta=run_meta,
        )
        self._runs[run.run_id] = run
        self._order.append(run.run_id)
        self._evict_terminal()
        return run

    def bind(self, run_id: str, task: "asyncio.Task") -> bool:
        """把运行实例与真实 ``asyncio.Task`` 绑定，使 ``cancel`` 能真正打断。

        **单 owner**：运行已绑定且既有任务仍在跑时**不改绑**（返回 ``False``
        并告警）——同一运行被两个执行体驱动会让 cancel 取错对象。既有任务
        已终了（``done()``）则允许改绑（重试/接管场景）。
        """
        run = self._runs.get(run_id)
        if run is None:
            return False
        existing = run._task
        if existing is not None and existing is not task and not existing.done():
            logger.warning(
                "bind refused: run {} already bound to a live task", run_id
            )
            return False
        run._task = task
        return True

    # -- 状态迁移 ------------------------------------------------------------

    def mark_running(self, run_id: str) -> bool:
        run = self._runs.get(run_id)
        if run is None or run.state.is_terminal:
            return False
        run.state = RunState.RUNNING
        run.started_at = time.time()
        return True

    def finish(
        self,
        run_id: str,
        *,
        state: RunState = RunState.SUCCEEDED,
        error: Optional[str] = None,
    ) -> bool:
        """标记终态。**已处于终态的运行不会被覆盖**（幂等，保护取消语义）。"""
        run = self._runs.get(run_id)
        if run is None or run.state.is_terminal:
            return False
        run.state = state
        run.error = error
        run.ended_at = time.time()
        return True

    async def cancel(self, run_id: str, *, reason: str = "user_cancelled") -> bool:
        """取消一次运行：置终态并（若已绑定）真正打断底层 Task。"""
        run = self._runs.get(run_id)
        if run is None or run.state.is_terminal:
            return False
        run.state = RunState.CANCELLED
        run.cancel_reason = reason
        run.ended_at = time.time()
        task = run._task
        if task is not None and not task.done():
            task.cancel()
            # 等它真正退出，避免「已报取消但仍在跑」的假象
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - 取消/异常都属预期
                pass
        logger.info("Agent run cancelled: run={} agent={} reason={}", run_id, run.agent_name, reason)
        return True

    # -- 查询 ---------------------------------------------------------------

    def status(self, run_id: str) -> Optional[AgentRun]:
        return self._runs.get(run_id)

    def list_runs(self, *, state: Optional[RunState] = None) -> List[AgentRun]:
        runs = [self._runs[rid] for rid in self._order if rid in self._runs]
        if state is not None:
            runs = [r for r in runs if r.state == state]
        return runs

    def active_count(self) -> int:
        return sum(1 for r in self._runs.values() if not r.state.is_terminal)

    def stats(self) -> Dict[str, Any]:
        by_state: Dict[str, int] = {}
        for run in self._runs.values():
            by_state[run.state.value] = by_state.get(run.state.value, 0) + 1
        return {
            "total": len(self._runs),
            "active": self.active_count(),
            "by_state": by_state,
            "max_runs": self._max_runs,
        }

    # -- 维护 ---------------------------------------------------------------

    def _evict_terminal(self) -> None:
        """超容时从最旧的**终态**记录开始淘汰（不淘汰运行中的）。"""
        while len(self._runs) > self._max_runs:
            for rid in list(self._order):
                run = self._runs.get(rid)
                if run is not None and run.state.is_terminal:
                    self._runs.pop(rid, None)
                    try:
                        self._order.remove(rid)
                    except ValueError:
                        pass
                    break
            else:
                return  # 全是运行中，放弃淘汰（宁可超容也不丢活动运行）

    def clear(self) -> None:
        self._runs.clear()
        self._order.clear()


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------

_plane: Optional[ControlPlane] = None


def get_control_plane() -> ControlPlane:
    global _plane
    if _plane is None:
        # 每次装配读 settings（Phase 25 准入三键；默认值 = 旧行为：
        # max_runs 500、并发/深度 0 = 不限）。
        from agentix.config import settings

        _plane = ControlPlane(
            max_runs=int(getattr(settings, "CONTROL_PLANE_MAX_RUNS", 500) or 500),
            max_concurrency=int(
                getattr(settings, "CONTROL_PLANE_MAX_CONCURRENCY", 0) or 0
            ),
            max_depth=int(getattr(settings, "CONTROL_PLANE_MAX_DEPTH", 0) or 0),
        )
    return _plane


def reset_control_plane() -> None:
    global _plane
    _plane = None
