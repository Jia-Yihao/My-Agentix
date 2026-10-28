"""R3.2 统一 Redis-Stack：DB 分片（db0 = cache + checkpoint / db1 = Celery）验证。

覆盖：
- checkpoint 分片必须是 db0——RediSearch 只能在 db0 建索引；R3.2 原设计放在 db2，
  2026-10-23 接真 Redis Stack 实测才暴露（asetup() 报 "Cannot create index on db != 0"
  ⇒ 静默降级 MemorySaver，而 URL 形状守卫一直是绿的）；
- cache 与 checkpoint 在 db0 共存的前提是 key 前缀不重叠，隔离靠命名空间不靠分库；
- retrieval 缓存默认委托统一 cache 分片（db0），连接失败优雅降级；
- Celery 保持「显式配置才启用」契约（默认空串 = 同步兜底）；
- integration：真连 Redis Stack 跑 aput→aget→alist 往返，证明没有静默降级。
"""

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest  # noqa: E402

from agentix.config import settings as unified_settings  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHECKPOINTER_SRC = PROJECT_ROOT / "agentix/infrastructure/persistence/checkpointer.py"
REDIS_CACHE_SRC = PROJECT_ROOT / "agentix/application/services/redis_cache.py"


def _db_of(url: str) -> str:
    return (urlsplit(url).path or "/0").lstrip("/") or "0"


def test_checkpoint_shard_default_is_db0():
    """RediSearch 硬约束：索引只能建在 db0，db!=0 ⇒ checkpointer 静默降级 MemorySaver。

    读的是**字段默认值**而不是 settings 实例：本机 .env 会覆盖实例值，
    用它做断言会让同一条守卫在 CI 与开发机上测出两种东西。
    """
    default = type(unified_settings).model_fields["REDIS_CHECKPOINT_URL"].default
    assert _db_of(default) == "0", (
        f"REDIS_CHECKPOINT_URL 默认值是 {default!r}（非 0 库）：asetup() 会报 "
        "'Cannot create index on db != 0'，checkpointer 静默退化成 MemorySaver"
        "——会话状态不再持久化，而应用启动仍然成功"
    )


def test_effective_checkpoint_url_is_db0_when_dotenv_present():
    """部署侧真正生效的是 .env（backend 挂载 ./.env:/app/.env）。
    本机 .env 若还停在 db2，这条会把「配置没跟着改」显式暴露出来。
    """
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        pytest.skip("本机无 .env，走代码默认值")
    m = re.search(r"(?m)^\s*#?\s*REDIS_CHECKPOINT_URL=(\S+)\s*$", env_path.read_text(encoding="utf-8"))
    if m is None:
        pytest.skip(".env 未显式设置 REDIS_CHECKPOINT_URL，走代码默认值")
    assert _db_of(m.group(1)) == "0", f".env 的 REDIS_CHECKPOINT_URL={m.group(1)} 不是 db0，checkpointer 会静默降级"


def test_celery_keeps_its_own_shard():
    fields = type(unified_settings).model_fields
    assert _db_of(fields["REDIS_CELERY_URL"].default) == "1"
    assert fields["REDIS_CELERY_URL"].default != fields["REDIS_CHECKPOINT_URL"].default


def test_db0_coexistence_is_isolated_by_key_prefix():
    """checkpoint 与 cache 同库的唯一前提是前缀不重叠；前缀漂移就要重审这个决定。

    前缀是 f-string 拼的（``f"{self.prefix}:{scope}"``），所以断言默认前缀实参而不是字面量。
    """
    cache_src = REDIS_CACHE_SRC.read_text(encoding="utf-8")
    for prefix in ('prefix: str = "semantic"', 'prefix: str = "history"'):
        assert prefix in cache_src, f"cache 默认前缀 {prefix!r} 变了，db0 共存隔离前提需重审"
    cp_src = CHECKPOINTER_SRC.read_text(encoding="utf-8")
    assert "settings.REDIS_CHECKPOINT_URL" in cp_src


def test_env_example_checkpoint_url_is_db0():
    """真正生效的是 .env（backend 挂载 ./.env:/app/.env）；示例文件退回 db2 就前功尽弃。"""
    env = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    m = re.search(r"(?m)^\s*REDIS_CHECKPOINT_URL=(\S+)\s*$", env)
    assert m is not None, ".env.example 里找不到 REDIS_CHECKPOINT_URL"
    assert _db_of(m.group(1)) == "0", f".env.example 的 REDIS_CHECKPOINT_URL={m.group(1)} 不是 db0"


def test_checkpointer_uses_checkpoint_shard():
    """回归守卫：checkpointer 必须读 REDIS_CHECKPOINT_URL，不得回落 REDIS_URL。"""
    src = CHECKPOINTER_SRC.read_text(encoding="utf-8")
    assert "settings.REDIS_CHECKPOINT_URL" in src


def test_retrieval_cache_defaults_to_cache_shard():
    from agentix.retrieval.settings import settings as retrieval_settings

    assert retrieval_settings.CACHE_REDIS_URL == unified_settings.REDIS_CACHE_URL


def test_celery_keeps_explicit_opt_in_contract():
    """默认空串 → enqueue 走同步兜底；部署侧经 .env 显式指向 db1。"""
    from agentix.retrieval.settings import settings as retrieval_settings

    env_broker = os.getenv("CELERY_BROKER_URL")
    if env_broker is None:
        assert retrieval_settings.CELERY_BROKER_URL == ""
        assert retrieval_settings.CELERY_RESULT_BACKEND == ""


def _restore_checkpointer_state(original_url: str) -> None:
    from agentix.infrastructure.persistence import checkpointer as cp

    unified_settings.REDIS_CHECKPOINT_URL = original_url
    unified_settings.CHECKPOINTER_TYPE = os.getenv("CHECKPOINTER_TYPE", "redis")
    cp.reset_checkpointer()


@pytest.mark.integration
def test_checkpointer_actually_activates_against_redis_stack():
    """能力测试：默认配置下 checkpointer 必须真是 AsyncRedisSaver 且能读写。

    这条测试存在的原因：db 形状守卫全绿时，真实 Redis 上它一次都没激活过。
    需要本机带 RediSearch 的 Redis Stack（docker compose up -d redis），默认跳过。
    """
    import asyncio
    import uuid

    from langgraph.checkpoint.base import empty_checkpoint

    from agentix.infrastructure.persistence import checkpointer as cp

    redis_url = os.getenv("AGENTIX_TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("需 AGENTIX_TEST_REDIS_URL 指向带 RediSearch 的 Redis，如 redis://127.0.0.1:6379/0")

    original_url = unified_settings.REDIS_CHECKPOINT_URL
    cp.reset_checkpointer()
    try:
        unified_settings.CHECKPOINTER_TYPE = "redis"
        unified_settings.REDIS_CHECKPOINT_URL = redis_url

        async def roundtrip():
            saver = await cp.init_checkpointer()
            assert type(saver).__name__ == "AsyncRedisSaver", (
                f"checkpointer 未激活：拿到 {type(saver).__name__}，"
                f"降级原因={cp.get_degraded_reason()!r}"
            )
            tid = f"r32-{uuid.uuid4().hex[:8]}"
            cfg = {"configurable": {"thread_id": tid, "checkpoint_ns": ""}}
            out = await saver.aput(
                cfg, empty_checkpoint(), {"step": 1, "source": "test", "writes": None}, {}
            )
            cid = out["configurable"]["checkpoint_id"]
            tup = await saver.aget_tuple(
                {"configurable": {"thread_id": tid, "checkpoint_ns": "", "checkpoint_id": cid}}
            )
            listed = [
                c async for c in saver.alist({"configurable": {"thread_id": tid, "checkpoint_ns": ""}})
            ]
            return tup is not None, len(listed)

        hit, listed = asyncio.run(roundtrip())
    finally:
        _restore_checkpointer_state(original_url)

    assert hit, "aput 之后 aget_tuple 读不回来"
    assert listed >= 1, "alist 查不到自己的 checkpoint（RediSearch 索引不在同一个库）"


@pytest.mark.integration
def test_redis_without_search_degrades_without_blocking_startup():
    """无 RediSearch 的 Redis 必须走降级分支（MemorySaver + 原因），而不是让应用起不来。

    2026-10-23 实测：官方 redis:7-alpine 连得上，但 asetup() 报 unknown command 'FT.INFO'。
    """
    import asyncio

    from agentix.infrastructure.persistence import checkpointer as cp

    plain_url = os.getenv("AGENTIX_TEST_PLAIN_REDIS_URL")
    if not plain_url:
        pytest.skip("需 AGENTIX_TEST_PLAIN_REDIS_URL 指向一个不带 RediSearch 的 Redis")

    original_url = unified_settings.REDIS_CHECKPOINT_URL
    cp.reset_checkpointer()
    try:
        unified_settings.CHECKPOINTER_TYPE = "redis"
        unified_settings.REDIS_CHECKPOINT_URL = plain_url
        saver = asyncio.run(cp.init_checkpointer())
        reason = cp.get_degraded_reason()
    finally:
        _restore_checkpointer_state(original_url)

    assert type(saver).__name__ in {"MemorySaver", "InMemorySaver"}
    assert reason and "FT." in reason
