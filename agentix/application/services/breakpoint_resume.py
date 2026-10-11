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
from enum import Enum
from typing import Any, Dict, List, Optional

from loguru import logger
from redis.asyncio import Redis

from agentix.config import settings


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


class CheckpointRecovery:
    """Crash recovery using LangGraph checkpointer + Redis state backup.

    On each graph step:
    1. LangGraph checkpointer saves state (MemorySaver/Redis)
    2. This module backs up the checkpoint metadata to Redis

    On restart:
    1. Check Redis for incomplete threads
    2. Resume from last checkpoint using graph.invoke(None, config)
    """

    def __init__(
        self,
        *,
        redis_client: Optional[Redis] = None,
        redis_url: Optional[str] = None,
        prefix: str = "checkpoint_recovery",
    ):
        self._redis = redis_client or Redis.from_url(
            redis_url or settings.REDIS_URL,
            decode_responses=True,
        )
        self.prefix = prefix

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

        if status == "in_progress":
            await self._redis.sadd(f"{self.prefix}:active_threads", thread_id)
        elif status in ("completed", "failed"):
            await self._redis.srem(f"{self.prefix}:active_threads", thread_id)

    async def get_active_threads(self) -> List[str]:
        """Get list of threads with incomplete executions."""
        members = await self._redis.smembers(f"{self.prefix}:active_threads")
        return list(members) if members else []

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
        """
        metadata = await self.get_checkpoint_metadata(thread_id)
        if not metadata:
            logger.info("No checkpoint found for thread {}", thread_id)
            return None

        config = {"configurable": {"thread_id": thread_id}}

        try:
            state = graph.get_state(config)
            if not state or not state.values:
                logger.warning("Empty checkpoint state for thread {}", thread_id)
                return None

            logger.info(
                "Resuming thread {} from node {}",
                thread_id,
                metadata.get("last_node", "unknown"),
            )

            result = await graph.ainvoke(None, config=config)

            await self.save_checkpoint_metadata(
                thread_id,
                "completed",
                {"message_count": len(result.get("messages", []))},
                status="completed",
            )

            return result

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
