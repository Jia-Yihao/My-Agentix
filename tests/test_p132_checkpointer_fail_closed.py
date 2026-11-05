"""P132：checkpointer 的 fail-closed 开关（Phase K 任务 1）测试。

覆盖：
- flag 关（默认）+ Redis 连接失败 ⇒ 降级 MemorySaver + 记录原因，行为与基线逐位不变；
- flag 开 + 同样的 ConnectionError ⇒ 抛 RuntimeError（消息以 "checkpointer fail-closed" 起头），
  且单例不得被粘成 MemorySaver（checkpointer.py 模块 docstring 第 3 条登记的"粘死"问题）；
- 显式 CHECKPOINTER_TYPE=memory + flag 开 ⇒ 正常返回 MemorySaver，不抛——显式配置不是故障。

异步用例照 tests/test_checkpointer_p0.py 的范式（@pytest.mark.asyncio + monkeypatch 桩）。
"""

import pytest
from langgraph.checkpoint.memory import MemorySaver

from agentix.config import settings as settings_instance
from agentix.infrastructure.persistence import checkpointer as ckpt_module


@pytest.fixture(autouse=True)
def _reset_singleton():
    """每个用例前后重置 checkpointer 单例，避免跨用例污染（含抛异常后的残留）。"""
    ckpt_module.reset_checkpointer()
    yield
    ckpt_module.reset_checkpointer()


def _patch_redis_connect_failure(monkeypatch):
    """伪造一个 asetup() 抛 ConnectionError 的 AsyncRedisSaver，命中 Redis 连接失败降级分支。"""

    class _FakeSaver:
        def __init__(self, **kwargs):
            pass

        async def asetup(self):
            raise ConnectionError("failed to connect to redis")

    monkeypatch.setattr(ckpt_module, "_try_import_async_redis_saver", lambda: _FakeSaver)

    import redis.asyncio as aioredis

    class _FakeRedis:
        @classmethod
        def from_url(cls, *args, **kwargs):
            return cls()

    monkeypatch.setattr(aioredis, "Redis", _FakeRedis)


@pytest.mark.asyncio
async def test_fail_closed_off_degrades_to_memory_saver_as_before(monkeypatch):
    """flag 关 + Redis 连不上 ⇒ MemorySaver + 降级原因非 None + 不抛（基线不变）。"""
    monkeypatch.setattr(settings_instance, "CHECKPOINTER_TYPE", "redis")
    # raising=False：实现前该字段尚不存在，等价于"默认关"；实现后显式钉住 False 排除宿主 .env 干扰
    monkeypatch.setattr(settings_instance, "CHECKPOINTER_FAIL_CLOSED", False, raising=False)
    _patch_redis_connect_failure(monkeypatch)

    saver = await ckpt_module.init_checkpointer()

    assert isinstance(saver, MemorySaver)
    assert ckpt_module.get_degraded_reason() is not None
    assert "ConnectionError" in ckpt_module.get_degraded_reason()
    # 降级后的单例照旧被粘住（基线行为，不应因新开关而改变）
    assert ckpt_module.get_checkpointer() is saver


@pytest.mark.asyncio
async def test_fail_closed_on_raises_and_keeps_singleton_unset(monkeypatch):
    """flag 开 + ConnectionError ⇒ 抛 RuntimeError，且抛完单例仍是 None（没被粘成 MemorySaver）。"""
    monkeypatch.setattr(settings_instance, "CHECKPOINTER_TYPE", "redis")
    monkeypatch.setattr(settings_instance, "CHECKPOINTER_FAIL_CLOSED", True, raising=False)
    _patch_redis_connect_failure(monkeypatch)

    with pytest.raises(RuntimeError) as excinfo:
        await ckpt_module.init_checkpointer()

    assert str(excinfo.value).startswith("checkpointer fail-closed: ")
    assert ckpt_module.get_checkpointer() is None


@pytest.mark.asyncio
async def test_explicit_memory_type_never_raises_even_when_fail_closed(monkeypatch):
    """显式 CHECKPOINTER_TYPE=memory + flag 开 ⇒ 正常返回 MemorySaver，不抛。"""
    monkeypatch.setattr(settings_instance, "CHECKPOINTER_TYPE", "memory")
    monkeypatch.setattr(settings_instance, "CHECKPOINTER_FAIL_CLOSED", True, raising=False)

    saver = await ckpt_module.init_checkpointer()

    assert isinstance(saver, MemorySaver)
    assert "explicit" in ckpt_module.get_degraded_reason()


@pytest.mark.asyncio
async def test_fail_closed_on_raises_when_redis_package_not_installed(monkeypatch):
    """flag 开 + langgraph-checkpoint-redis 未安装 ⇒ 抛 RuntimeError，单例不被粘成 MemorySaver。

    这条钉的是 init_checkpointer 的第一条 fail-closed 出口（"未安装"分支）：既有用例的桩
    全部返回一个假 saver 类，走不到 AsyncRedisSaver is None 那条判断，删掉该出口不会被发现。
    """
    monkeypatch.setattr(settings_instance, "CHECKPOINTER_TYPE", "redis")
    monkeypatch.setattr(settings_instance, "CHECKPOINTER_FAIL_CLOSED", True, raising=False)
    # 返回 None ⇒ 命中「未安装」分支（checkpointer.py 的 `if AsyncRedisSaver is None`）
    monkeypatch.setattr(ckpt_module, "_try_import_async_redis_saver", lambda: None)

    with pytest.raises(RuntimeError) as excinfo:
        await ckpt_module.init_checkpointer()

    assert str(excinfo.value).startswith(
        "checkpointer fail-closed: langgraph-checkpoint-redis not installed"
    )
    # 抛之前不得给 _saver 赋值，否则单例粘死成 MemorySaver（见模块 docstring 第 3 条）
    assert ckpt_module.get_checkpointer() is None
