"""
Async task queue backed by Redis Stream.

Supports:
- Task submission (producer)
- Task consumption with consumer groups (worker)
- Task status tracking (pending/processing/completed/failed)
- Retry with exponential backoff
- Dead letter queue for permanently failed tasks
"""

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, Optional

from loguru import logger
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from agentix.config import settings


class TaskStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD = "dead"


@dataclass
class Task:
    task_id: str
    task_type: str
    payload: Dict[str, Any]
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 3
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "payload": self.payload,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Task":
        return cls(
            task_id=data["task_id"],
            task_type=data["task_type"],
            payload=data["payload"],
            status=TaskStatus(data.get("status", "pending")),
            result=data.get("result"),
            error=data.get("error"),
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 3),
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
            completed_at=data.get("completed_at"),
        )


class TaskQueue:
    """Redis Stream-based async task queue."""

    def __init__(
        self,
        *,
        redis_client: Optional[Redis] = None,
        redis_url: Optional[str] = None,
        stream_name: str = "agentix:tasks",
        consumer_group: str = "workers",
        consumer_name: Optional[str] = None,
    ):
        self._redis = redis_client or Redis.from_url(
            redis_url or settings.REDIS_URL,
            decode_responses=True,
        )
        self.stream_name = stream_name
        self.consumer_group = consumer_group
        self.consumer_name = consumer_name or f"worker-{uuid.uuid4().hex[:8]}"
        self._handlers: Dict[str, Callable[..., Coroutine]] = {}
        self._running = False
        # 持有 fire-and-forget 任务引用，防 asyncio 任务被 GC 提前回收。
        self._background_tasks: set = set()

    def _spawn_background(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def register(self, task_type: str):
        """Decorator to register a task handler."""
        def decorator(func: Callable[..., Coroutine]):
            self._handlers[task_type] = func
            logger.info("Registered task handler: {} -> {}", task_type, func.__name__)
            return func
        return decorator

    async def submit(
        self,
        task_type: str,
        payload: Dict[str, Any],
        *,
        max_retries: int = 3,
    ) -> Task:
        """Submit a new task to the queue.

        ``SET`` + ``XADD`` 走同一 MULTI/EXEC：此前两次独立调用，若在中间
        崩溃会留下「任务记录在、流里没有」的孤儿（永远无人消费），或反过来
        「流里有、记录查不到」（worker 直接 ack 丢弃）。
        """
        task = Task(
            task_id=uuid.uuid4().hex,
            task_type=task_type,
            payload=payload,
            max_retries=max_retries,
        )
        task.updated_at = time.time()

        pipe = self._redis.pipeline(transaction=True)
        pipe.set(
            f"task:{task.task_id}",
            json.dumps(task.to_dict()),
            ex=86400 * 7,
        )
        pipe.xadd(
            self.stream_name,
            {"task_id": task.task_id, "task_type": task_type, "payload": json.dumps(payload)},
        )
        await pipe.execute()

        logger.info("Task submitted: id={} type={}", task.task_id, task_type)
        return task

    async def get_task(self, task_id: str) -> Optional[Task]:
        """Get task status by ID."""
        raw = await self._redis.get(f"task:{task_id}")
        if not raw:
            return None
        return Task.from_dict(json.loads(raw))

    async def _save_task(self, task: Task) -> None:
        """Persist task state to Redis."""
        task.updated_at = time.time()
        await self._redis.set(
            f"task:{task.task_id}",
            json.dumps(task.to_dict()),
            ex=86400 * 7,
        )

    async def _ensure_consumer_group(self) -> None:
        """Create consumer group if it doesn't exist.

        幂等语义：组已存在时 Redis 返回 ``BUSYGROUP``，属预期，降为 debug。

        其余失败**必须留痕**：此前这里是 ``except Exception: pass``，于是
        Redis 不可达 / NOAUTH / 权限不足时本方法"成功"返回，紧接着
        ``xreadgroup`` 抛 ``NOGROUP``——运维看到的是「消费组不存在」这个
        **误导性**结论，而真实原因是「连不上 Redis」。错误里的因果被这一行
        ``pass`` 抹掉了。修法只补日志、**不改控制流**（start_worker 的
        循环本就带重试与 error 日志，改抛异常会改变启动期语义）。
        """
        try:
            await self._redis.xgroup_create(
                self.stream_name, self.consumer_group, id="0", mkstream=True
            )
            logger.info(
                "Created consumer group: stream={} group={}",
                self.stream_name,
                self.consumer_group,
            )
        except ResponseError as exc:
            if "BUSYGROUP" in str(exc):
                logger.debug(
                    "Consumer group already exists: stream={} group={}",
                    self.stream_name,
                    self.consumer_group,
                )
            else:
                logger.warning(
                    "Failed to ensure consumer group: stream={} group={} err={}",
                    self.stream_name,
                    self.consumer_group,
                    exc,
                )
        except Exception as exc:  # 连接/鉴权类失败：不中断启动，但要可见
            logger.warning(
                "Failed to ensure consumer group (will surface later at xreadgroup): "
                "stream={} group={} err={}",
                self.stream_name,
                self.consumer_group,
                exc,
            )

    async def start_worker(self, batch_size: int = 1, block_ms: int = 5000) -> None:
        """Start consuming tasks from the stream."""
        await self._ensure_consumer_group()
        self._running = True
        logger.info(
            "Task worker started: consumer={} group={}",
            self.consumer_name,
            self.consumer_group,
        )

        while self._running:
            try:
                messages = await self._redis.xreadgroup(
                    self.consumer_group,
                    self.consumer_name,
                    {self.stream_name: ">"},
                    count=batch_size,
                    block=block_ms,
                )

                if not messages:
                    continue

                for stream_name, entries in messages:
                    for message_id, data in entries:
                        await self._process_message(message_id, data)

            except asyncio.CancelledError:
                logger.info("Task worker cancelled")
                break
            except Exception as exc:
                logger.error("Task worker error: {}", exc)
                await asyncio.sleep(1)

    async def stop_worker(self) -> None:
        """Stop the worker loop."""
        self._running = False

    async def _process_message(self, message_id: str, data: Dict[str, str]) -> None:
        """Process a single message from the stream."""
        task_id = data.get("task_id")
        task_type = data.get("task_type")

        task = await self.get_task(task_id)
        if not task:
            logger.warning("Task not found: {}", task_id)
            await self._redis.xack(self.stream_name, self.consumer_group, message_id)
            return

        handler = self._handlers.get(task_type)
        if not handler:
            logger.error("No handler for task type: {}", task_type)
            task.status = TaskStatus.FAILED
            task.error = f"No handler for task type: {task_type}"
            await self._save_task(task)
            await self._redis.xack(self.stream_name, self.consumer_group, message_id)
            return

        # Phase H · W3：负载甩脱（LOAD_SHEDDING_ENABLED，默认关 = 零判定）。
        # 消费侧甩——已入队任务在执行前按阶梯丢弃（后台优先：evolution/eval
        # 最先），标记 FAILED 留痕 + ack，不进重试分支、不执行 handler。
        if bool(getattr(settings, "LOAD_SHEDDING_ENABLED", False)):
            from agentix.performance.load_shedder import get_load_shedder

            shedder = get_load_shedder()
            if shedder.should_shed(task_type):
                task.status = TaskStatus.FAILED
                task.error = f"load shed: {shedder.current_level().name}"
                await self._save_task(task)
                await self._redis.xack(
                    self.stream_name, self.consumer_group, message_id
                )
                logger.warning(
                    "task shed by load shedding: id={} type={} level={}",
                    task_id,
                    task_type,
                    shedder.current_level().name,
                )
                return

        task.status = TaskStatus.PROCESSING
        await self._save_task(task)

        try:
            result = await handler(task.payload)
            task.status = TaskStatus.COMPLETED
            task.result = result if isinstance(result, dict) else {"data": result}
            task.completed_at = time.time()
            await self._save_task(task)
            logger.info("Task completed: id={} type={}", task_id, task_type)

        except Exception as exc:
            task.retry_count += 1
            logger.error(
                "Task failed: id={} type={} retry={}/{} error={}",
                task_id,
                task_type,
                task.retry_count,
                task.max_retries,
                exc,
            )

            if task.retry_count >= task.max_retries:
                task.status = TaskStatus.DEAD
                task.error = str(exc)
                await self._save_task(task)
                await self._redis.rpush(
                    f"{self.stream_name}:dead",
                    json.dumps(task.to_dict()),
                )
                logger.warning("Task moved to dead letter queue: {}", task_id)
            else:
                task.status = TaskStatus.PENDING
                task.error = str(exc)
                await self._save_task(task)
                backoff = min(2 ** task.retry_count, 60)
                # 先 ack 再退避：原消息已处理完，PEL 不该在 sleep 期间挂着。
                # 此前 sleep 在 ack 之前，单 worker 会卡满 backoff 秒、
                # 后续消息全部积压；进程在 sleep 中崩溃还会触发 XAUTOCLAIM
                # 重复消费。
                await self._redis.xack(self.stream_name, self.consumer_group, message_id)
                self._spawn_background(
                    self._delayed_requeue(task, task_type, backoff)
                )
                return

        await self._redis.xack(self.stream_name, self.consumer_group, message_id)

    async def _delayed_requeue(
        self, task: Task, task_type: str, delay: float
    ) -> None:
        """Backoff 后把任务重新写回流（后台执行，不阻塞 worker 循环）。"""
        cancelled = False
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            # 关停时仍尝试立即重投，避免任务停在 PENDING 却不在流里。
            cancelled = True
        try:
            await self._redis.xadd(
                self.stream_name,
                {
                    "task_id": task.task_id,
                    "task_type": task_type,
                    "payload": json.dumps(task.payload),
                },
            )
            logger.info(
                "Task requeued: id={} delay={} cancelled={}",
                task.task_id,
                delay,
                cancelled,
            )
        except Exception as exc:
            logger.error("Failed to requeue task id={}: {}", task.task_id, exc)
            try:
                task.status = TaskStatus.FAILED
                task.error = f"requeue failed: {exc}"
                await self._save_task(task)
            except Exception:
                pass
        if cancelled:
            raise asyncio.CancelledError


_task_queue_instance: Optional[TaskQueue] = None


def get_task_queue() -> TaskQueue:
    """Get or create the global task queue instance."""
    global _task_queue_instance
    if _task_queue_instance is None:
        _task_queue_instance = TaskQueue()
    return _task_queue_instance
