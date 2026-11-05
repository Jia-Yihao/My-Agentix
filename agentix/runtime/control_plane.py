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

**T21 持久化（默认关）**：``RUN_STATE_PERSIST_ENABLED=true`` 时运行登记与每次状态
迁移经**有界后台写队列 + 单写线程**落 ``agent_run_states``，使 ``GET /agents/runs``
在重启后仍可查历史终态。装配判据唯一源 :func:`resolve_run_state_store`（flag-off
连持久层都不 import、不建表、不读写）。写库失败 **fail-open**：只 warning +
``stats().persist_errors`` 计数，运行照跑、内存态不回滚（约定 2）。
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Deque, Dict, List, Optional

from loguru import logger

if TYPE_CHECKING:
    # 注解专用：``AgentRun.budget``/``spawn(budget=)`` 两处皆字符串注解
    # （``from __future__ import annotations``），运行期零引用——``to_dict()``
    # 走鸭子类型。保持 Runtime 簇**运行期自包含**：M2-3 拆边后 TokenBudget
    # 规范位在 ``domain/runtime/token_budget.py``（纯数据下沉，application 侧
    # 为再导出 shim），连 TYPE_CHECKING 边也不再指向 application
    # （先例：``runtime/state.py`` 对 ``steps`` 的 TYPE_CHECKING 回指）。
    from agentix.domain.runtime.token_budget import TokenBudget

__all__ = [
    "RunState",
    "AgentRun",
    "AdmissionDenied",
    "ControlPlane",
    "get_control_plane",
    "reset_control_plane",
    "resolve_run_state_store",
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

    def to_record(self) -> Dict[str, Any]:
        """落库投影（T21）：与 :meth:`to_dict` 的**响应面分开**。

        差异是刻意的：库记录不承载 ``budget``（进程内预算对象，重启后没有归属
        意义）、不承载 ``duration_ms``（由 ``started_at``/``ended_at`` 即时算出），
        但带 ``updated_at``（列表回落排序键）与 ``run_depth``（登记时
        ``meta.depth`` 的列化）。``meta`` 是浅拷贝：写线程与事件循环并发读它时
        不共享同一个可变字典。
        """
        return {
            "run_id": self.run_id,
            "agent_name": self.agent_name,
            "state": self.state.value,
            "trace_id": self.trace_id,
            "meta": dict(self.meta),
            "run_depth": int(self.meta.get("depth", 0) or 0),
            "created_at": self.created_at,
            "updated_at": time.time(),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "error": self.error,
            "cancel_reason": self.cancel_reason,
        }

    @classmethod
    def from_record(cls, record: Dict[str, Any]) -> "AgentRun":
        """库记录 → 运行实例（重启后 GET 回落的装配点）。

        只用于**读**：装配出的实例不进 ``ControlPlane._runs``（启动恢复不要求灌回
        内存，回落即可），因此 ``_task``/``budget`` 恒空。状态词表外的行
        （坏数据）抛 ``ValueError``，由调用方决定降级向哪。
        """
        data = dict(record or {})
        return cls(
            run_id=str(data.get("run_id") or ""),
            agent_name=str(data.get("agent_name") or ""),
            state=RunState(str(data.get("state") or RunState.PENDING.value)),
            trace_id=str(data.get("trace_id") or "-"),
            budget=None,
            created_at=float(data.get("created_at") or time.time()),
            started_at=data.get("started_at"),
            ended_at=data.get("ended_at"),
            error=data.get("error"),
            cancel_reason=data.get("cancel_reason"),
            meta=dict(data.get("meta") or {}),
        )


class _RunStateWriter:
    """有界写队列 + 单写线程（T21）：同步 SQLAlchemy I/O **绝不在事件循环里跑**。

    为什么是后台线程而不是每次 ``to_thread``：写入点是 ``spawn`` / ``mark_running``
    / ``finish`` / ``cancel``——全都是同步方法（调用方含非 async 路径），在同步方法
    里 ``await to_thread`` 无从谈起；改成异步会改掉四个既有方法的签名（波及
    ``chat_service`` 三个调用点），代价远大于收益。单写线程还顺带把「同一 run 的
    多次迁移」串成序（spawn → running → 终态 的写入顺序天然正确）。

    **有界**：队列超过 ``max_queue`` 时**丢最旧并计数**——运行态观测面不能因为库慢
    把进程内存撑穿（约定 2 的 fail-open 方向是「少记一条」而不是「拖垮主链」）。

    失败姿态：``save_run`` 抛错只 ``logger.warning`` + ``errors += 1``；**绝不**向上
    冒泡、**绝不**回滚内存态（内存面才是运行的第一步真相，库是它的投影）。
    """

    def __init__(self, store: Any, *, max_queue: int = 256) -> None:
        self._store = store
        self._max_queue = max(1, int(max_queue))
        self._items: Deque[Dict[str, Any]] = deque()
        self._cond = threading.Condition()
        self._busy = False
        self._closed = False
        #: 观测计数（只在 flag-on 的 ``stats()`` 中出现；无高基数维度）
        self.written = 0
        self.errors = 0
        self.dropped = 0
        self._thread = threading.Thread(
            target=self._loop, name="agentix-run-state-writer", daemon=True
        )
        self._thread.start()

    def submit(self, record: Dict[str, Any]) -> None:
        """入队（不阻塞调用方；满则丢最旧）。"""
        with self._cond:
            if self._closed:
                return
            while len(self._items) >= self._max_queue:
                self._items.popleft()
                self.dropped += 1
            self._items.append(record)
            self._cond.notify()

    def _loop(self) -> None:
        while True:
            with self._cond:
                while not self._items and not self._closed:
                    self._cond.wait()
                if not self._items:  # closed 且已排空
                    return
                record = self._items.popleft()
                self._busy = True
            try:
                self._store.save_run(record)
            except Exception as exc:  # noqa: BLE001 - 降级去向：只 warning+计数，运行继续、内存态不回滚
                with self._cond:
                    self.errors += 1
                    self._busy = False
                    self._cond.notify_all()
                logger.warning(
                    "agent run state persist failed for {} (run continues in memory, "
                    "db row not written): {}",
                    record.get("run_id"),
                    exc,
                )
                continue
            with self._cond:
                self.written += 1
                self._busy = False
                self._cond.notify_all()

    def flush(self, timeout_s: float = 2.0) -> bool:
        """等待队列排空且当前一条写完；``False`` = 超时（调用方自行决定是否 warning）。"""
        with self._cond:
            return self._cond.wait_for(
                lambda: not self._items and not self._busy, timeout=float(timeout_s)
            )

    def snapshot(self) -> Dict[str, int]:
        with self._cond:
            return {
                "queued": len(self._items),
                "written": self.written,
                "errors": self.errors,
                "dropped": self.dropped,
            }

    def close(self, timeout_s: float = 2.0) -> None:
        """停止接收新记录，尽力排空后退出线程（关停/测试清理用）。"""
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        self._thread.join(timeout=float(timeout_s))


class ControlPlane:
    """进程内 Agent 运行登记表 + 取消 + 预算归属 + 准入。"""

    def __init__(
        self,
        *,
        max_runs: int = 500,
        max_concurrency: int = 0,
        max_depth: int = 0,
        run_store: Optional[Any] = None,
        persist_queue_max: int = 256,
    ) -> None:
        self._runs: Dict[str, AgentRun] = {}
        self._order: List[str] = []
        self._max_runs = max(1, int(max_runs))
        #: 并发准入上限：``0`` = 不限（默认 = 接线前行为）
        self._max_concurrency = max(0, int(max_concurrency))
        #: 深度准入上限：``0`` = 不限
        self._max_depth = max(0, int(max_depth))
        #: T21 运行态库面（``RunStateStore`` 鸭子型）；None = 纯内存基线（flag-off）。
        #: 装配判定只在 :func:`resolve_run_state_store` 一处，本类不再读 flag。
        self._run_store = run_store
        self._writer: Optional[_RunStateWriter] = (
            _RunStateWriter(run_store, max_queue=persist_queue_max)
            if run_store is not None
            else None
        )

    @property
    def run_store(self) -> Optional[Any]:
        """T21 库面；None = 纯内存基线（也是「flag 是否开」在平面内的唯一判据）。"""
        return self._run_store

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
        self._enqueue_snapshot(run)
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
        self._enqueue_snapshot(run)
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
        self._enqueue_snapshot(run)
        return True

    async def cancel(self, run_id: str, *, reason: str = "user_cancelled") -> bool:
        """取消一次运行：置终态并（若已绑定）真正打断底层 Task。"""
        run = self._runs.get(run_id)
        if run is None or run.state.is_terminal:
            return False
        run.state = RunState.CANCELLED
        run.cancel_reason = reason
        run.ended_at = time.time()
        # 终态必须先投递再等底层任务退出：task.cancel() 之后的 await 可能很久
        # （执行器忽略取消），而「已取消」这个事实不该跟着一起等。
        self._enqueue_snapshot(run)
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
        stats: Dict[str, Any] = {
            "total": len(self._runs),
            "active": self.active_count(),
            "by_state": by_state,
            "max_runs": self._max_runs,
        }
        if self._writer is not None:
            # 计数键**只在 flag-on 时存在**：flag-off 的 stats() 键集合必须与基线
            # 逐字相同（约定 1），所以不预先塞 0。无高基数 label（约定 3）。
            snapshot = self._writer.snapshot()
            stats.update(
                {
                    "persist_queued": snapshot["queued"],
                    "persist_written": snapshot["written"],
                    "persist_errors": snapshot["errors"],
                    "persist_dropped": snapshot["dropped"],
                }
            )
        return stats

    # -- 持久化（T21） -------------------------------------------------------

    def persist_run(self, run_id: str) -> bool:
        """把当前内存快照补投给库面；``False`` = 无库面（flag-off）或运行不在内存。

        ``finish`` 之后仍可能补写 ``meta``（如 ``_execute_run`` 落 ``result``），
        响应面看到的 meta 与库行必须一致，故调用方在 mutate 后显式补投一次。
        flag-off 时本方法是**零副作用**的 ``False``（不 import、不建表、不排队）。
        """
        run = self._runs.get(run_id)
        if run is None or self._writer is None:
            return False
        self._enqueue_snapshot(run)
        return True

    def _enqueue_snapshot(self, run: AgentRun) -> None:
        """把一条运行快照投进写队列（flag-off ⇒ 立即返回，零新副作用）。"""
        if self._writer is None:
            return
        self._writer.submit(run.to_record())

    def flush_persist(self, timeout_s: float = 2.0) -> bool:
        """等待写队列排空（测试/关停用）；无库面 ⇒ ``True``（无事可等）。"""
        if self._writer is None:
            return True
        return self._writer.flush(timeout_s)

    def close_persist(self, timeout_s: float = 2.0) -> None:
        """停止写线程（单例重建/关停）。无库面 ⇒ 空操作。"""
        if self._writer is None:
            return
        self._writer.close(timeout_s)

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


def resolve_run_state_store() -> Optional[Any]:
    """T21 装配判定**唯一源**：``RUN_STATE_PERSIST_ENABLED=false``（默认）恒 ``None``。

    flag-off 时连 store 模块都不 import——不触 sqlalchemy、不建池、不建表、不读写
    新表（约定 1 的「逐位等价」要求装配点本身零新副作用，而不是「建了但不用」）。
    flag 判据只在这里读一次；``get_control_plane``、测试注入与 ``runs.py`` 的
    「库里有没有面」判定都只认这一处（约定 6 ④：同一个判定不许两个源）。
    """
    from agentix.config import settings

    if not bool(getattr(settings, "RUN_STATE_PERSIST_ENABLED", False)):
        return None
    from agentix.infrastructure.persistence.run_state_store import (
        build_default_run_state_store,
    )

    return build_default_run_state_store()


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
            # T21：flag-off 得到 None = 与基线构造逐位等价（不启写线程）
            run_store=resolve_run_state_store(),
        )
    return _plane


def reset_control_plane() -> None:
    global _plane
    if _plane is not None:
        # 写线程是 daemon，但重建单例时仍要**尽力排空**再放手：测试与关停路径
        # 不该留下一条还在往库里写的线程。
        _plane.close_persist()
    _plane = None
