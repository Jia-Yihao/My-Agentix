"""Unit tests for task queue module."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agentix.application.services.task_queue import (
    Task,
    TaskQueue,
    TaskStatus,
)


class TestTask:
    """Tests for Task dataclass."""

    def test_task_creation(self):
        task = Task(task_id="abc123", task_type="file_import", payload={"path": "/test"})
        assert task.task_id == "abc123"
        assert task.task_type == "file_import"
        assert task.status == TaskStatus.PENDING
        assert task.retry_count == 0
        assert task.max_retries == 3

    def test_task_to_dict(self):
        task = Task(task_id="abc", task_type="test", payload={"key": "val"})
        d = task.to_dict()
        assert d["task_id"] == "abc"
        assert d["task_type"] == "test"
        assert d["status"] == "pending"
        assert d["payload"] == {"key": "val"}

    def test_task_from_dict(self):
        data = {
            "task_id": "xyz",
            "task_type": "batch_embedding",
            "payload": {"docs": []},
            "status": "completed",
            "retry_count": 2,
            "max_retries": 5,
        }
        task = Task.from_dict(data)
        assert task.task_id == "xyz"
        assert task.status == TaskStatus.COMPLETED
        assert task.retry_count == 2
        assert task.max_retries == 5

    def test_task_roundtrip(self):
        original = Task(
            task_id="round",
            task_type="test",
            payload={"a": 1},
            status=TaskStatus.PROCESSING,
            retry_count=1,
        )
        restored = Task.from_dict(original.to_dict())
        assert restored.task_id == original.task_id
        assert restored.status == original.status
        assert restored.retry_count == original.retry_count


class TestTaskStatus:
    """Tests for TaskStatus enum."""

    def test_all_statuses(self):
        assert TaskStatus.PENDING.value == "pending"
        assert TaskStatus.PROCESSING.value == "processing"
        assert TaskStatus.COMPLETED.value == "completed"
        assert TaskStatus.FAILED.value == "failed"
        assert TaskStatus.DEAD.value == "dead"


class TestTaskQueueRegistration:
    """Tests for task handler registration."""

    def test_register_handler(self):
        queue = TaskQueue.__new__(TaskQueue)
        queue._handlers = {}

        @queue.register("test_task")
        async def handler(payload):
            return {"ok": True}

        assert "test_task" in queue._handlers
        assert queue._handlers["test_task"] is handler

    def test_register_multiple_handlers(self):
        queue = TaskQueue.__new__(TaskQueue)
        queue._handlers = {}

        @queue.register("task_a")
        async def handler_a(payload):
            return "a"

        @queue.register("task_b")
        async def handler_b(payload):
            return "b"

        assert len(queue._handlers) == 2
        assert "task_a" in queue._handlers
        assert "task_b" in queue._handlers


class TestSubmitAtomicity:
    """submit 必须用 pipeline 原子写 SET + XADD（防半提交孤儿）。"""

    @pytest.mark.asyncio
    async def test_submit_uses_single_pipeline(self):
        queue = TaskQueue.__new__(TaskQueue)
        queue.stream_name = "agentix:tasks"
        queue._handlers = {}
        queue._background_tasks = set()

        pipe = MagicMock()
        pipe.set = MagicMock()
        pipe.xadd = MagicMock()
        pipe.execute = AsyncMock(return_value=[True, True])

        redis = MagicMock()
        redis.pipeline = MagicMock(return_value=pipe)
        queue._redis = redis

        task = await queue.submit("file_import", {"path": "/tmp/x.xlsx"})

        redis.pipeline.assert_called_once_with(transaction=True)
        pipe.set.assert_called_once()
        pipe.xadd.assert_called_once()
        pipe.execute.assert_awaited_once()
        # 不得再走非原子的独立 set/xadd
        redis.set.assert_not_called()
        redis.xadd.assert_not_called()
        assert task.task_type == "file_import"


class TestRetryDoesNotBlockWorker:
    """重试路径：先 ack，再后台退避重投（sleep 不得卡在 xack 之前）。"""

    @pytest.mark.asyncio
    async def test_retry_acks_then_schedules_background_requeue(self):
        queue = TaskQueue.__new__(TaskQueue)
        queue.stream_name = "agentix:tasks"
        queue.consumer_group = "workers"
        queue.consumer_name = "w1"
        queue._handlers = {}
        queue._background_tasks = set()

        task = Task(
            task_id="t1",
            task_type="file_import",
            payload={"path": "/tmp/x"},
            max_retries=3,
            retry_count=0,
        )
        stored = json.dumps(task.to_dict())

        redis = MagicMock()
        redis.get = AsyncMock(return_value=stored)
        redis.set = AsyncMock()
        redis.xack = AsyncMock()
        redis.xadd = AsyncMock()
        redis.rpush = AsyncMock()
        queue._redis = redis

        async def boom(payload):
            raise RuntimeError("handler failed")

        queue._handlers["file_import"] = boom

        # 捕获 spawn 的协程，不让它真的 sleep
        spawned = []

        def fake_spawn(coro):
            spawned.append(coro)
            return MagicMock()

        queue._spawn_background = fake_spawn

        await queue._process_message(
            "1-0",
            {"task_id": "t1", "task_type": "file_import", "payload": "{}"},
        )

        redis.xack.assert_awaited_once_with(
            "agentix:tasks", "workers", "1-0"
        )
        assert len(spawned) == 1
        # 关闭协程，避免 "never awaited" 警告
        spawned[0].close()

    @pytest.mark.asyncio
    async def test_delayed_requeue_xadd_after_sleep(self):
        queue = TaskQueue.__new__(TaskQueue)
        queue.stream_name = "agentix:tasks"
        queue._handlers = {}
        queue._background_tasks = set()

        task = Task(task_id="t2", task_type="file_import", payload={"a": 1})
        redis = MagicMock()
        redis.xadd = AsyncMock()
        redis.set = AsyncMock()
        queue._redis = redis

        with patch(
            "agentix.application.services.task_queue.asyncio.sleep",
            new=AsyncMock(),
        ):
            await queue._delayed_requeue(task, "file_import", 2)

        redis.xadd.assert_awaited_once()
        args = redis.xadd.await_args
        assert args.args[0] == "agentix:tasks"
        assert args.args[1]["task_id"] == "t2"
