"""
Async task queue backed by Redis Stream.

Supports:
- Task submission (producer)
- Task consumption with consumer groups (worker)
- PEL（Pending Entries List）认领：死消费者遗留消息经 XAUTOCLAIM 有界接手
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
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from loguru import logger
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from agentix.config import settings
from agentix.observability.metrics import record_task_queue_claimed

# ─────────────────────── PEL 认领（XAUTOCLAIM）三个模块常量 ───────────────────────
# 为什么要认领：consumer group 的语义是「读走即进 PEL，处理完才 XACK」。消费者
# 崩在 `xreadgroup` 与 `xack` 之间（或被 kill -9），那条消息就永远挂在 PEL 里，
# 同一个 group 的 `>` 通道再也不会把它派给任何人 ⇒ 任务永久卡死。XAUTOCLAIM 是
# Redis 给的唯一正规接管手段（Redis ≥ 6.2，本仓 compose 用
# `redis/redis-stack-server:7.4.0-v2`，满足）。
#
# 三个值取模块常量的理由（不新增配置键）：它们只在「补漏」这条支路上生效，
# 调错也不会改变正常消费路径，不值得再背一套「配置键五处同步」的成本。

#: 只认领 **idle ≥ 60 秒** 的消息。为什么是 60 秒：正常的单条消息处理（含 handler
#: 里的外部 HTTP）通常远在 60 秒内 ack；阈值取太小会把「正在处理、只是还没 ack」
#: 的消息抢过来，等于人为制造重复执行（本队列的 handler 不是幂等的：file_import
#: 会打外部 ingest 服务）。60 秒同时与启动恢复的 `min_idle_seconds=60` 口径同量级，
#: 运维只需要记一个数字。
_CLAIM_IDLE_MS = 60_000

#: 认领周期（秒）：不要在每一轮消费循环里都 XAUTOCLAIM。为什么是 30 秒：
#: 认领是「补漏」而不是「主通道」，比 idle 阈值短太多会反复扫同一批还没到期的
#: 消息（每轮一次 XAUTOCLAIM = 拿 Redis 空转）；取 idle 阈值的一半，最坏多等
#: 半个周期就能接手一条到期消息，且占用可忽略。
_CLAIM_INTERVAL_S = 30.0

#: 单轮认领条数上限。为什么不一次拖光：死消费者可能留下上千条 PEL，一轮认领
#: 全部拖过来会把它们全塞进 PEL（认领 = 换个 owner），随后 processing 状态逐个
#: 写 Redis，形成一次人为的瞬时洪峰。16 = 一批能在一轮里做完、又不至于把
#: 一次 block 周期占满的量级；认领不完的**下一轮继续**（这是设计意图，不是漏）。
_CLAIM_MAX_PER_ROUND = 16


def _normalize_xautoclaim_response(raw: Any) -> Tuple[str, List[Tuple[str, Dict[str, str]]]]:
    """把 ``XAUTOCLAIM`` 的两种返回形状归一成 ``(next_start_id, entries)``。

    **为什么必须兼容两种形状**：返回值形状由**服务端** Redis 版本决定，不由
    redis-py 版本单独决定——
    - Redis 6.2 起：``[next-cursor, entries]``（2 元组）；
    - Redis 7.0 起：``[next-cursor, entries, deleted-ids]``（3 元组，多一段
      「认领时发现已被删除的消息 id」）。

    窗口期里 redis-py 只是把服务端原始响应透传（``parse_xautoclaim`` 只解析
    ``response[1]``），所以同一份代码连 6.2 与 7.x 会看到两种长度。这里做**一处**
    归一：统一丢掉第三段（删除的 id 不需要处理——它们已不在流里，ack 也无意义），
    调用方只认 ``(next_start_id, entries)``。第二段里值为 ``None`` 的条目
    （Redis 7 早期把已删除消息放在 entries 里的形态）一并剔除，否则
    ``_process_message`` 会在 ``data.get`` 上抛 ``AttributeError``。
    """
    if not raw:
        return "0-0", []
    try:
        next_start_id = raw[0]
        entries = raw[1] or []
    except (IndexError, TypeError, KeyError):
        # 形状完全意外（桩/代理返回了别的东西）：当作"没有可认领的消息"，
        # 由调用方计一次 empty，不在这里抛——认领是补漏件，不该打死消费循环。
        logger.warning(
            "Unexpected XAUTOCLAIM response shape: type={} raw={!r}",
            type(raw).__name__,
            raw,
        )
        return "0-0", []
    cleaned: List[Tuple[str, Dict[str, str]]] = [
        (message_id, data) for message_id, data in entries if data is not None
    ]
    return str(next_start_id), cleaned


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
        """消费循环：``xreadgroup`` 取新消息 + 周期性 ``XAUTOCLAIM`` 接手死消费者的 PEL。

        **本方法不读任何 flag**：要不要跑由调用方（``main._start_task_queue_worker``）
        判定一次（纪律 6 ④：同一个判定不许两个源）。

        **上界是什么**（验收①「有界」）：认领每 ``_CLAIM_INTERVAL_S`` 秒最多一轮、
        每轮最多 ``_CLAIM_MAX_PER_ROUND`` 条 ⇒ 单次运行内认领次数上界是
        ``⌈运行时长 / _CLAIM_INTERVAL_S⌉``、条数上界是上述次数 ×
        ``_CLAIM_MAX_PER_ROUND``。**刻意不设"整个进程生命周期最多认领 N 次"这类
        累计上限**：worker 是长期运行的，累计上限用尽之后 PEL 就永久不再被接手——
        那正是本卡要消灭的那个 bug 换了个样子。一次认领不完的留给下一轮，是设计意图。
        """
        await self._ensure_consumer_group()
        self._running = True
        logger.info(
            "Task worker started: consumer={} group={}",
            self.consumer_name,
            self.consumer_group,
        )

        # 启动即做一次认领：上一次进程可能就死在 PEL 里，先接手再开始读新消息。
        next_claim_at = time.monotonic()
        while self._running:
            try:
                if time.monotonic() >= next_claim_at:
                    # 先推进计时再认领：认领本身抛异常时不会变成"每轮都重试"
                    # （那会把一次 Redis 抖动放大成持续压力）。
                    next_claim_at = time.monotonic() + _CLAIM_INTERVAL_S
                    await self._claim_stale_pending()

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
                # 取消必须能被调用方观察到（``Task.cancelled()`` 为真）：吞掉
                # CancelledError 时关停只看得到「正常返回」，无法区分「被取消」与
                # 「自然结束」——测试与排障都失去判据。
                raise
            except Exception as exc:
                logger.error("Task worker error: {}", exc)
                await asyncio.sleep(1)

    async def stop_worker(self) -> None:
        """请求消费循环退出。

        只翻 ``_running``，**不强杀**：当下那次 ``xreadgroup(block=block_ms)``
        要自然到期才返回（默认最多 5 秒）。调用方若不想等这 5 秒，接着
        ``cancel()`` 任务即可——两个动作都留在调用方（``_shutdown``）里，
        本方法保持"只表达意图"的单一职责。
        """
        self._running = False

    async def _claim_stale_pending(self) -> int:
        """做**一轮** ``XAUTOCLAIM``，把认领到的消息交给**同一份** ``_process_message``。

        返回本轮实际交给处理器的条数（供调用方/测试断言；0 表示没有可认领消息）。
        **不复制处理逻辑**（纪律 6 ④）：认领来的消息与新消息在语义上完全一致，
        走两套处理代码只会让「ack / 重试 / dead letter / 甩负荷」慢慢漂移。

        失败姿态：``XAUTOCLAIM`` 自身失败**只留痕 + 记指标后返回 0**，异常不上抛——
        认领是补漏件，一次 Redis 抖动不该把消费循环打断（外层 ``except Exception``
        会 ``sleep(1)`` 再继续，那等于让**新消息**也陪着一起等）。消费循环因此
        始终活着，下一轮继续尝试认领。
        """
        try:
            raw = await self._redis.xautoclaim(
                self.stream_name,
                self.consumer_group,
                self.consumer_name,
                min_idle_time=_CLAIM_IDLE_MS,
                start_id="0-0",
                count=_CLAIM_MAX_PER_ROUND,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 认领失败降级为「本轮不认领」，留痕 + 指标，下一轮再试（纪律 2）
            record_task_queue_claimed("error")
            logger.warning(
                "PEL claim failed (retrying next round): stream={} group={} consumer={} "
                "err_type={} err={}",
                self.stream_name,
                self.consumer_group,
                self.consumer_name,
                type(exc).__name__,
                exc,
            )
            return 0

        _next_start_id, entries = _normalize_xautoclaim_response(raw)
        if not entries:
            record_task_queue_claimed("empty")
            return 0
        # 防御性截断：``count=`` 已经限过，但服务端/代理忽略 COUNT 时不能让一轮
        # 认领无界膨胀（有界是验收①的一半）。
        if len(entries) > _CLAIM_MAX_PER_ROUND:
            entries = entries[:_CLAIM_MAX_PER_ROUND]

        claimed = 0
        for message_id, data in entries:
            await self._process_message(message_id, data)
            # 庚-3 ζ（台账 :1374 收口）：`ok` **逐条**记——一条成功交接即入账一次。
            # 旧形态在整轮之后才记一枚，轮中 `_process_message` 抛出时本轮已交接的
            # 消息既不记 ok 也不记 error（穿出本函数后外层只 logger.error），恰好
            # 吞掉最该观测的「交接了一半就断了」。`empty`/`error` 保持轮级：它们
            # 本来就是「这一轮没消息 / 这一轮 XAUTOCLAIM 失败」，没有逐条形态。
            record_task_queue_claimed("ok")
            claimed += 1
        logger.info(
            "PEL claimed: stream={} group={} consumer={} count={}",
            self.stream_name,
            self.consumer_group,
            self.consumer_name,
            claimed,
        )
        return claimed

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
