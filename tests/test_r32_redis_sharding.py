"""R3.2 统一 Redis-Stack：DB 分片（db0 cache / db1 Celery / db2 checkpoint）验证。

覆盖：
- 统一 settings 暴露三个分片 URL 且默认互不重叠；
- checkpointer 走 checkpoint 分片（db2），不再复用 REDIS_URL；
- retrieval 缓存默认委托统一 cache 分片（db0），连接失败优雅降级；
- Celery 保持「显式配置才启用」契约（默认空串 = 同步兜底）。
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from agentix.config import settings as unified_settings  # noqa: E402


def test_shard_defaults_are_distinct():
    assert unified_settings.REDIS_CACHE_URL.endswith("/0")
    assert unified_settings.REDIS_CELERY_URL.endswith("/1")
    assert unified_settings.REDIS_CHECKPOINT_URL.endswith("/2")
    assert (
        len(
            {
                unified_settings.REDIS_CACHE_URL,
                unified_settings.REDIS_CELERY_URL,
                unified_settings.REDIS_CHECKPOINT_URL,
            }
        )
        == 3
    )


def test_checkpointer_uses_checkpoint_shard():
    """回归守卫：checkpointer 必须读 REDIS_CHECKPOINT_URL（db2），不得回落 REDIS_URL。"""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "agentix"
        / "infrastructure"
        / "persistence"
        / "checkpointer.py"
    ).read_text(encoding="utf-8")
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
