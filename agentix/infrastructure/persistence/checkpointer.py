"""LangGraph Checkpointer 装配（P0-1）。

采用 LangGraph 官方子包 langgraph-checkpoint-redis 的 AsyncRedisSaver，
替代 lg_builder.py 模块级的 MemorySaver，使会话状态跨进程、跨重启持久化。

关键约束：
  1. Redis 必须支持 RediSearch（否则 asetup() 抛 unknown command 'FT.CREATE'）——
     docker-compose.yml 已换用 redis/redis-stack-server 镜像。
  2. asetup() 是协程，必须在应用启动阶段 await 一次；它是幂等的，可重复调用。

降级设计依据方案 [第5章]（恢复优先级：重试 → 回滚 → 替代路径 → 熔断 → 人工升级）：
Redis 不可用属于"替代路径"场景——
  - langgraph-checkpoint-redis 未安装 → 降级 MemorySaver
  - settings.CHECKPOINTER_TYPE == "memory" → 显式使用 MemorySaver
  - Redis 连接/asetup 失败 → 降级 MemorySaver
保证核心问答仍可用，而非整体启动失败。
"""

from typing import Optional, Union

from langgraph.checkpoint.memory import MemorySaver
from loguru import logger

from agentix.config import settings

_saver: Optional[Union[object, MemorySaver]] = None
_degraded_reason: Optional[str] = None


def _try_import_async_redis_saver():
    """兼容不同版本的导入路径（aio 子模块 / 包根）。"""
    try:
        from langgraph.checkpoint.redis.aio import AsyncRedisSaver  # noqa: PLC0415

        return AsyncRedisSaver
    except ImportError:
        pass
    try:
        from langgraph.checkpoint.redis import AsyncRedisSaver  # noqa: PLC0415

        return AsyncRedisSaver
    except ImportError:
        return None


async def init_checkpointer():
    """初始化并返回 checkpointer 单例（AsyncRedisSaver 或降级的 MemorySaver）。

    必须在 FastAPI 的 startup/lifespan 中调用一次（因为 asetup() 是协程）。
    任何失败都降级为 MemorySaver 并记录原因，绝不抛出异常阻塞启动。
    """
    global _saver, _degraded_reason

    if _saver is not None:
        return _saver

    if settings.CHECKPOINTER_TYPE == "memory":
        _saver = MemorySaver()
        _degraded_reason = "CHECKPOINTER_TYPE=memory (explicit)"
        logger.warning("checkpointer: using MemorySaver (explicit config)")
        _record_degraded("checkpointer")
        return _saver

    AsyncRedisSaver = _try_import_async_redis_saver()
    if AsyncRedisSaver is None:
        _saver = MemorySaver()
        _degraded_reason = "langgraph-checkpoint-redis not installed"
        logger.warning(
            "checkpointer: langgraph-checkpoint-redis not installed, "
            "falling back to MemorySaver (session state will not persist)"
        )
        _record_degraded("checkpointer")
        return _saver

    try:
        from redis.asyncio import Redis  # noqa: PLC0415

        # R3.2：checkpoint 落独立分片 db2（RediSearch 索引与 cache db0 隔离）
        redis_client = Redis.from_url(settings.REDIS_CHECKPOINT_URL, decode_responses=False)
        saver = AsyncRedisSaver(
            redis_client=redis_client,
            ttl={"default_ttl": 60 * 24 * 7, "refresh_on_read": True},  # 分钟 → 7 天
        )
        await saver.asetup()  # 创建 RediSearch 索引，幂等
        _saver = saver
        _degraded_reason = None
        logger.info("Redis checkpointer initialized (langgraph-checkpoint-redis)")
        return _saver
    except Exception as exc:  # noqa: BLE001 - 降级路径，见模块 docstring
        _saver = MemorySaver()
        _degraded_reason = f"{type(exc).__name__}: {exc}"
        logger.error(
            "checkpointer init failed, fallback to MemorySaver: {} — "
            "core Q&A remains available; session state will not persist",
            _degraded_reason,
        )
        _record_degraded("checkpointer")
        return _saver


def _record_degraded(component: str) -> None:
    """企业化 P1-2.1：降级事件上报 Prometheus（失败不影响主链路）。"""
    try:
        from agentix.observability.metrics import record_degraded

        record_degraded(component)
    except Exception:  # noqa: BLE001
        pass


def get_checkpointer():
    """取已初始化的 checkpointer；未初始化返回 None（表示不持久化）。"""
    return _saver


def get_degraded_reason() -> Optional[str]:
    """返回降级原因；None 表示 Redis checkpointer 正常生效。"""
    return _degraded_reason


def reset_checkpointer() -> None:
    """重置单例（仅供测试使用）。"""
    global _saver, _degraded_reason
    _saver = None
    _degraded_reason = None
