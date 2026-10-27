"""
Agent breakpoint resume (断点续跑) module.

Implements LangGraph's interrupt/resume mechanism for:
1. Human-in-the-loop approval before executing sensitive operations
2. Graceful pause on external service failures with auto-retry
3. Checkpoint-based state persistence for crash recovery
4. Step-by-step execution with manual override capability

Architecture:
  - interrupt_before: Pause BEFORE a node executes (for approval)
  - interrupt_after:  Pause AFTER a node executes (for review)
  - Command(resume=...): Resume from interrupt with optional data injection
  - Checkpointer: Persists state at every step for crash recovery

Usage patterns:
  1. Auto-resume: Agent pauses on failure, auto-retries after backoff
  2. Human approval: Agent pauses before tool execution, waits for human OK
  3. Crash recovery: Process dies, restart picks up from last checkpoint
"""

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from loguru import logger
from redis.asyncio import Redis

from agentix.config import settings
from agentix.observability.spans import record_resume_from_checkpoint


class InterruptReason(str, Enum):
    HUMAN_APPROVAL = "human_approval"
    SERVICE_FAILURE = "service_failure"
    RATE_LIMIT = "rate_limit"
    MANUAL_PAUSE = "manual_pause"
    TIMEOUT = "timeout"


class ResumeAction(str, Enum):
    CONTINUE = "continue"
    RETRY = "retry"
    SKIP = "skip"
    ABORT = "abort"
    OVERRIDE = "override"


@dataclass
class InterruptRecord:
    """Record of an agent interrupt event."""
    interrupt_id: str
    thread_id: str
    node_name: str
    reason: InterruptReason
    state_snapshot: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 3
    created_at: float = field(default_factory=time.time)
    resolved_at: Optional[float] = None
    resolution: Optional[ResumeAction] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "interrupt_id": self.interrupt_id,
            "thread_id": self.thread_id,
            "node_name": self.node_name,
            "reason": self.reason.value,
            "state_snapshot": self.state_snapshot,
            "error": self.error,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "resolution": self.resolution.value if self.resolution else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "InterruptRecord":
        return cls(
            interrupt_id=data["interrupt_id"],
            thread_id=data["thread_id"],
            node_name=data["node_name"],
            reason=InterruptReason(data["reason"]),
            state_snapshot=data.get("state_snapshot", {}),
            error=data.get("error"),
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 3),
            created_at=data.get("created_at", time.time()),
            resolved_at=data.get("resolved_at"),
            resolution=ResumeAction(data["resolution"]) if data.get("resolution") else None,
        )


class BreakpointManager:
    """Manages agent interrupts and resume operations.

    Provides:
    - Interrupt recording and tracking
    - Auto-retry with exponential backoff
    - Human approval workflow
    - State inspection and override
    - Crash recovery from Redis-backed checkpoints
    """

    def __init__(
        self,
        *,
        redis_client: Optional[Redis] = None,
        redis_url: Optional[str] = None,
        prefix: str = "breakpoint",
    ):
        self._redis = redis_client or Redis.from_url(
            redis_url or settings.REDIS_URL,
            decode_responses=True,
        )
        self.prefix = prefix

    async def record_interrupt(self, record: InterruptRecord) -> str:
        """Record a new interrupt event."""
        key = f"{self.prefix}:{record.interrupt_id}"
        await self._redis.set(key, json.dumps(record.to_dict()), ex=86400 * 7)

        thread_key = f"{self.prefix}:thread:{record.thread_id}:interrupts"
        await self._redis.rpush(thread_key, record.interrupt_id)
        await self._redis.ltrim(thread_key, -100, -1)
        await self._redis.expire(thread_key, 86400 * 7)

        logger.info(
            "Interrupt recorded: id={} thread={} node={} reason={}",
            record.interrupt_id,
            record.thread_id,
            record.node_name,
            record.reason.value,
        )
        return record.interrupt_id

    async def get_interrupt(self, interrupt_id: str) -> Optional[InterruptRecord]:
        """Get interrupt record by ID."""
        raw = await self._redis.get(f"{self.prefix}:{interrupt_id}")
        if not raw:
            return None
        return InterruptRecord.from_dict(json.loads(raw))

    async def get_thread_interrupts(
        self, thread_id: str, unresolved_only: bool = True
    ) -> List[InterruptRecord]:
        """Get all interrupts for a thread."""
        thread_key = f"{self.prefix}:thread:{thread_id}:interrupts"
        interrupt_ids = await self._redis.lrange(thread_key, 0, -1)

        records: List[InterruptRecord] = []
        for iid in interrupt_ids:
            record = await self.get_interrupt(iid)
            if record:
                if unresolved_only and record.resolved_at is not None:
                    continue
                records.append(record)

        return records

    async def resolve_interrupt(
        self,
        interrupt_id: str,
        action: ResumeAction,
        *,
        override_data: Optional[Dict[str, Any]] = None,
    ) -> Optional[InterruptRecord]:
        """Resolve an interrupt with a specific action.

        Args:
            interrupt_id: The interrupt to resolve.
            action: How to resume (continue/retry/skip/abort/override).
            override_data: Optional data to inject when using OVERRIDE action.

        Returns:
            Updated interrupt record, or None if not found.
        """
        record = await self.get_interrupt(interrupt_id)
        if not record:
            logger.warning("Interrupt not found: {}", interrupt_id)
            return None

        record.resolved_at = time.time()
        record.resolution = action

        if action == ResumeAction.RETRY:
            record.retry_count += 1
            if record.retry_count > record.max_retries:
                logger.warning(
                    "Max retries exceeded for interrupt {}: {}/{}",
                    interrupt_id,
                    record.retry_count,
                    record.max_retries,
                )
                record.resolution = ResumeAction.ABORT

        await self._redis.set(
            f"{self.prefix}:{interrupt_id}",
            json.dumps(record.to_dict()),
            ex=86400 * 7,
        )

        logger.info(
            "Interrupt resolved: id={} action={} retry={}",
            interrupt_id,
            action.value,
            record.retry_count,
        )
        return record


# ---------------------------------------------------------------------------
# LG-2-1：待恢复线程由 checkpointer.alist() 派生（不再读 Redis 集合）
# ---------------------------------------------------------------------------
#
# 换源头的理由：`active_threads` 这个 Redis 集合在生产上**没有写入点**——唯一的
# 写方是本模块的 save_checkpoint_metadata，而它只在恢复路径被调用，恢复的前提
# 又是集合里已经有线程（自举不成立）。于是启动恢复与 /ops/active-threads 长期
# 恒返回空，断点续跑等于没接线。checkpoint 本身每一步都落，是唯一可靠的
# 「哪些线程被跑到一半」的痕迹。
#
# 「未完成」判据只从图状态快照取（next / interrupts），不读 checkpoint 元数据里的
# writes：2026-10-27 实测，「跑完」与「断在中途」两类线程各自的最新 checkpoint 都是
# pending_writes=[]，用它判会把两类全判成完成。

#: 单页扫描量：alist(None) 是跨线程全量列举，线程数增长后必须带 limit。
THREAD_SCAN_PAGE_SIZE = 200
#: 总翻页上限：恢复只关心「最近一段」未完成线程，不做穷举（5 × 200 条 checkpoint）。
#: 到顶即停并打 debug 日志，不是静默截断。
THREAD_SCAN_MAX_PAGES = 5


async def _state_snapshot(graph: Any, config: Dict[str, Any]) -> Any:
    """读线程最新状态快照：优先原生异步接口，退回同步接口。

    编译图两者都有，优先 aget_state 以免阻塞事件循环；测试桩只实现同步
    get_state（tests/test_p133_decision_span_attributes.py），走回退分支。
    """
    aget_state = getattr(graph, "aget_state", None)
    if aget_state is not None:
        return await aget_state(config)
    return graph.get_state(config)


def _snapshot_is_unfinished(snapshot: Any) -> bool:
    """「未完成」唯一判据：还有待执行节点，或挂着未决中断。

    next 覆盖静态 interrupt_before 与节点内 interrupt() 两种暂停；interrupts 兜住
    next 为空但确有中断待处理的情形。
    """
    return bool(getattr(snapshot, "next", ()) or getattr(snapshot, "interrupts", ()))


def _idle_seconds(snapshot: Any) -> Optional[float]:
    """最新快照距今秒数；取不到（无 created_at / 解析失败）返回 None。"""
    raw = getattr(snapshot, "created_at", None)
    if not raw:
        return None
    try:
        created = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds()


def _pending_interrupts(snapshot: Any) -> tuple:
    """快照上挂着的中断；空元组 = 没有待人工处理的中断。"""
    return tuple(getattr(snapshot, "interrupts", ()) or ())


class ThreadInterruptedError(RuntimeError):
    """线程挂着未决中断：它等的是人工裁决，不是崩溃恢复（LG-2-2）。

    刻意不用 ``ainvoke(None)`` 硬续：实测那样会「200 但图又挂回去」，把「需要人」
    报成「已恢复」。端点据此回 409 并指向 ``/ops/hitl/in-loop``，启动恢复据此跳过。
    """

    def __init__(self, thread_id: str, pending: int):
        super().__init__(f"thread {thread_id} has {pending} pending interrupt(s)")
        self.thread_id = thread_id
        self.pending = pending


class CheckpointRecovery:
    """Crash recovery driven by the LangGraph checkpointer itself.

    On each graph step the checkpointer saves state (MemorySaver / Redis). The
    Redis metadata written by this module is bookkeeping only — since LG-2-1 it
    is no longer the source of "which threads are unfinished".

    On restart:
    1. Enumerate thread ids from checkpointer.alist (bounded, paged)
    2. Judge "unfinished" from the graph's own snapshot (next / interrupts)
    3. Resume with graph.ainvoke(None, config)
    """

    def __init__(
        self,
        *,
        redis_client: Optional[Redis] = None,
        redis_url: Optional[str] = None,
        checkpointer: Any = None,
        prefix: str = "checkpoint_recovery",
    ):
        self._redis = redis_client or Redis.from_url(
            redis_url or settings.REDIS_URL,
            decode_responses=True,
        )
        self._checkpointer = checkpointer
        self.prefix = prefix

    def _resolve_checkpointer(self) -> Any:
        """取 checkpointer：显式注入优先，否则读装配层单例。

        每次调用现读模块属性（不在 import 期绑定值）：单例由 init_checkpointer()
        在启动期才赋值，import 期取到的会是 None。
        """
        if self._checkpointer is not None:
            return self._checkpointer
        try:
            from agentix.infrastructure.persistence.checkpointer import get_checkpointer

            return get_checkpointer()
        except Exception as exc:  # noqa: BLE001 - 取不到与「未初始化」同义：降级并留痕（纪律 2）
            logger.warning("checkpointer lookup failed: {}", exc)
            return None

    async def _iter_checkpoint_thread_ids(self) -> List[str]:
        """按 checkpoint 时间倒序枚举线程 id（去重、翻页有界）。

        只读 alist 元组的 config——这里回答的是「哪些线程有 checkpoint」；线程是否
        未完成由调用方用图状态判定（纪律 6 ④：判据与执行链同源）。
        """
        checkpointer = self._resolve_checkpointer()
        if checkpointer is None:
            logger.warning(
                "thread scan skipped: checkpointer unavailable "
                "(init_checkpointer not called / assembly not ready)"
            )
            return []

        thread_ids: List[str] = []
        seen: set = set()
        before: Optional[Dict[str, Any]] = None
        exhausted = False
        for page in range(THREAD_SCAN_MAX_PAGES):
            batch = []
            try:
                async for tup in checkpointer.alist(
                    None, before=before, limit=THREAD_SCAN_PAGE_SIZE
                ):
                    batch.append(tup)
            except Exception as exc:  # noqa: BLE001 - 降级必须留痕，不许静默返空（LG-2-1 验收③）
                logger.warning(
                    "checkpointer.alist failed on page {} (threads found so far: {}): {}",
                    page,
                    len(thread_ids),
                    exc,
                )
                break
            for tup in batch:
                thread_id = tup.config["configurable"]["thread_id"]
                if thread_id not in seen:
                    seen.add(thread_id)
                    thread_ids.append(thread_id)
            if len(batch) < THREAD_SCAN_PAGE_SIZE:
                exhausted = True
                break
            before = batch[-1].config

        if not exhausted:
            logger.debug(
                "thread scan hit page cap ({} x {}), older checkpoints not scanned",
                THREAD_SCAN_MAX_PAGES,
                THREAD_SCAN_PAGE_SIZE,
            )
        return thread_ids

    async def get_thread_snapshot(self, thread_id: str, graph: Any) -> Any:
        """取线程最新快照；无 checkpoint 或状态为空 ⇒ None。

        这是「该线程有没有可续跑状态」的唯一判据：/ops/threads/resume 的 404 与
        resume_thread 的前置检查都走它（纪律 6 ④，不许两处各判一遍）。
        """
        config = {"configurable": {"thread_id": thread_id}}
        try:
            snapshot = await _state_snapshot(graph, config)
        except Exception as exc:  # noqa: BLE001 - 读不到与「没有」同义：返回 None 由调用方定状态码
            logger.warning("Failed to read state snapshot for thread {}: {}", thread_id, exc)
            return None
        if snapshot is None or not getattr(snapshot, "values", None):
            return None
        return snapshot

    async def get_active_threads(
        self,
        graph: Any,
        *,
        min_idle_seconds: float = 0.0,
    ) -> List[str]:
        """列出「跑到一半」的线程（潜在崩溃受害者 / 待人工处理的挂起）。

        Args:
            graph: 编译图——「未完成」判据取自图自己的状态快照，不另取一份。
            min_idle_seconds: 只返回最新快照距今至少这么多秒的线程。启动恢复用它
                避开「此刻仍有进程在跑」的线程；原实现按 Redis 元数据的 updated_at
                判 60 秒，而该元数据无生产写入点（见文件头），这里换成 checkpoint
                自带的 created_at——同一意图、同一条时间轴。0 表示不过滤。

        Returns:
            线程 id 列表（按 checkpoint 时间倒序，已去重）。
        """
        active: List[str] = []
        for thread_id in await self._iter_checkpoint_thread_ids():
            config = {"configurable": {"thread_id": thread_id}}
            try:
                snapshot = await _state_snapshot(graph, config)
            except Exception as exc:  # noqa: BLE001 - 单线程读失败不拖垮整轮扫描（纪律 2）
                logger.warning(
                    "state snapshot unavailable for thread {}: {}", thread_id, exc
                )
                continue
            if not _snapshot_is_unfinished(snapshot):
                continue
            if min_idle_seconds > 0:
                idle = _idle_seconds(snapshot)
                if idle is None or idle < min_idle_seconds:
                    continue
            active.append(thread_id)
        return active

    async def save_checkpoint_metadata(
        self,
        thread_id: str,
        node_name: str,
        state_summary: Dict[str, Any],
        *,
        status: str = "in_progress",
    ) -> None:
        """Save checkpoint metadata for crash recovery."""
        key = f"{self.prefix}:{thread_id}"
        metadata = {
            "thread_id": thread_id,
            "last_node": node_name,
            "state_summary": state_summary,
            "status": status,
            "updated_at": time.time(),
        }
        await self._redis.set(key, json.dumps(metadata), ex=86400 * 3)
        # LG-2-1：不再维护 `active_threads` 集合——该集合在生产上没有写入点
        # （本方法是唯一写方，却只在恢复路径被调用，自举不成立）。待恢复线程
        # 改由 checkpointer.alist() 派生，见 get_active_threads。

    async def get_checkpoint_metadata(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """Get checkpoint metadata for a thread."""
        raw = await self._redis.get(f"{self.prefix}:{thread_id}")
        if not raw:
            return None
        return json.loads(raw)

    async def resume_thread(self, thread_id: str, graph) -> Optional[Dict[str, Any]]:
        """Attempt to resume a thread from its last checkpoint.

        Args:
            thread_id: The thread to resume.
            graph: The compiled LangGraph graph.

        Returns:
            The final state after resumption, or None if no checkpoint found.

        Raises:
            ThreadInterruptedError: 线程挂着未决中断（LG-2-2）。这类线程不能被
                ``ainvoke(None)`` 续跑，调用方应改走 ``/ops/hitl/in-loop``。
        """
        config = {"configurable": {"thread_id": thread_id}}

        try:
            # LG-2-1：前置判据是 checkpoint 快照本身，不是 Redis 元数据——待恢复
            # 线程由 checkpointer 派生，它们没有（也从来不会有）元数据。
            snapshot = await self.get_thread_snapshot(thread_id, graph)
            if snapshot is None:
                logger.warning("No resumable checkpoint for thread {}", thread_id)
                return None

            # LG-2-2：挂着未决中断的线程等的是人工裁决——治理层等待 ≠ 崩溃。这里抛给
            # 调用方（端点 409 / 启动跳过），不写 failed 元数据、不动图状态。
            pending = _pending_interrupts(snapshot)
            if pending:
                logger.info(
                    "Thread {} has {} pending interrupt(s); resume requires /ops/hitl/in-loop",
                    thread_id,
                    len(pending),
                )
                raise ThreadInterruptedError(thread_id, len(pending))

            try:
                metadata = await self.get_checkpoint_metadata(thread_id) or {}
            except Exception as exc:  # noqa: BLE001 - 元数据只补日志，读不到不影响续跑（纪律 2）
                logger.warning(
                    "checkpoint metadata unavailable for thread {}: {}", thread_id, exc
                )
                metadata = {}

            logger.info(
                "Resuming thread {} from node {}",
                thread_id,
                metadata.get("last_node", "unknown"),
            )

            # PhaseK 任务 6：断点续跑恢复点落 span。thread_id 判据只从 config 取
            # （唯一真源，纪律 6 ④；thread_id = session_id，见 application/services/
            # chat_service.py）；fail-open 由 helper 保证，绝不影响恢复主链路（纪律 2）。
            record_resume_from_checkpoint(thread_id_present=bool(config["configurable"].get("thread_id")))

            result = await graph.ainvoke(None, config=config)

            await self.save_checkpoint_metadata(
                thread_id,
                "completed",
                {"message_count": len(result.get("messages", []))},
                status="completed",
            )

            return result

        except ThreadInterruptedError:
            # 上游信号：不写 failed 元数据、不改任何状态（裁决入口是 /ops/hitl/in-loop）
            raise
        except Exception as exc:
            logger.error("Failed to resume thread {}: {}", thread_id, exc)
            await self.save_checkpoint_metadata(
                thread_id,
                "error",
                {"error": str(exc)},
                status="failed",
            )
            return None


_breakpoint_manager_instance: Optional[BreakpointManager] = None
_checkpoint_recovery_instance: Optional[CheckpointRecovery] = None


def get_breakpoint_manager() -> BreakpointManager:
    global _breakpoint_manager_instance
    if _breakpoint_manager_instance is None:
        _breakpoint_manager_instance = BreakpointManager()
    return _breakpoint_manager_instance


def get_checkpoint_recovery() -> CheckpointRecovery:
    global _checkpoint_recovery_instance
    if _checkpoint_recovery_instance is None:
        _checkpoint_recovery_instance = CheckpointRecovery()
    return _checkpoint_recovery_instance


# ---------------------------------------------------------------------------
# P4（岗位对照·异常闭环 4.4）：失败 turn durable 边界（FAILURE_BOUNDARY_ENABLED）
# ---------------------------------------------------------------------------

#: 显式排除的错误码：上下文溢出类失败**不**补写边界消息——上下文管理自身
#: 的动作（压缩/截断/预算收敛）不伪装成「请求失败 turn」，且写进去也只对
#: 恢复无信息量（hermes 源同样按类排除，见 source-code-study §5 4.4）。
FAILURE_BOUNDARY_EXCLUDED_CODES = frozenset({"context_overflow"})


def failure_boundary_content(
    error_code: Optional[str], *, fallback: str = ""
) -> Optional[str]:
    """失败 turn 的 assistant 边界文案（该失败可补写才返回文本，否则 ``None``）。

    - 排除码（``FAILURE_BOUNDARY_EXCLUDED_CODES``）⇒ ``None``（不补写）；
    - 其余 ⇒ ``[对话边界] 本轮请求失败（{code}）：{fallback}``——历史里
      user 消息后紧跟一条**可解释失败**的 assistant 消息，消除 user-user
      相邻（hermes ``conversation_loop.py:1745`` 的 durable failure boundary；
      发布侧遵守「先持久化后发布」，``cli_session_mixin.py:753``）。
    """
    code = str(error_code or "").strip() or "unknown_error"
    if code in FAILURE_BOUNDARY_EXCLUDED_CODES:
        return None
    text = str(fallback or "").strip()
    return f"[对话边界] 本轮请求失败（{code}）：{text or '请求未完成'}"
