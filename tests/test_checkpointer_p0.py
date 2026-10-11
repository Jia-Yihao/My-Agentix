"""P0-1 Checkpointer 装配测试。

覆盖：
- settings.CHECKPOINTER_TYPE=memory 显式降级
- langgraph-checkpoint-redis 未安装时降级 MemorySaver
- Redis 连接/asetup 失败时降级 MemorySaver
- 成功路径返回 AsyncRedisSaver（mock）
- lg_builder.build_graph 工厂（None / MemorySaver 均可编译）
"""

import pytest
from langgraph.checkpoint.memory import MemorySaver

from agentix.config import settings as settings_instance
from agentix.infrastructure.persistence import checkpointer as ckpt_module


@pytest.fixture(autouse=True)
def _reset_singleton():
    """每个用例前后重置 checkpointer 单例，避免跨用例污染。"""
    ckpt_module.reset_checkpointer()
    yield
    ckpt_module.reset_checkpointer()


class TestInitCheckpointerDegradation:
    """降级路径：任何失败都返回 MemorySaver，绝不抛异常。"""

    @pytest.mark.asyncio
    async def test_explicit_memory_config(self):
        settings_instance.CHECKPOINTER_TYPE = "memory"
        saver = await ckpt_module.init_checkpointer()
        assert isinstance(saver, MemorySaver)
        assert "explicit" in ckpt_module.get_degraded_reason()

    @pytest.mark.asyncio
    async def test_package_not_installed_falls_back(self, monkeypatch):
        settings_instance.CHECKPOINTER_TYPE = "redis"
        monkeypatch.setattr(ckpt_module, "_try_import_async_redis_saver", lambda: None)
        saver = await ckpt_module.init_checkpointer()
        assert isinstance(saver, MemorySaver)
        assert "not installed" in ckpt_module.get_degraded_reason()

    @pytest.mark.asyncio
    async def test_redis_failure_falls_back(self, monkeypatch):
        settings_instance.CHECKPOINTER_TYPE = "redis"

        class _FakeSaver:
            def __init__(self, **kwargs):
                pass

            async def asetup(self):
                raise RuntimeError("unknown command 'FT.CREATE'")

        monkeypatch.setattr(
            ckpt_module, "_try_import_async_redis_saver", lambda: _FakeSaver
        )

        import redis.asyncio as aioredis

        class _FakeRedis:
            @classmethod
            def from_url(cls, *args, **kwargs):
                return cls()

        monkeypatch.setattr(aioredis, "Redis", _FakeRedis)

        saver = await ckpt_module.init_checkpointer()
        assert isinstance(saver, MemorySaver)
        assert "FT.CREATE" in ckpt_module.get_degraded_reason()

    @pytest.mark.asyncio
    async def test_success_path_returns_saver(self, monkeypatch):
        settings_instance.CHECKPOINTER_TYPE = "redis"

        class _FakeSaver:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.setup_called = False

            async def asetup(self):
                self.setup_called = True

        fake_instance = _FakeSaver()

        def _factory(**kwargs):
            fake_instance.kwargs.update(kwargs)
            return fake_instance

        monkeypatch.setattr(ckpt_module, "_try_import_async_redis_saver", lambda: _factory)

        import redis.asyncio as aioredis

        class _FakeRedis:
            @classmethod
            def from_url(cls, *args, **kwargs):
                return cls()

        monkeypatch.setattr(aioredis, "Redis", _FakeRedis)

        saver = await ckpt_module.init_checkpointer()
        assert saver is fake_instance
        assert saver.setup_called is True
        assert ckpt_module.get_degraded_reason() is None

    @pytest.mark.asyncio
    async def test_singleton_reuses_initialized_saver(self):
        settings_instance.CHECKPOINTER_TYPE = "memory"
        first = await ckpt_module.init_checkpointer()
        second = await ckpt_module.init_checkpointer()
        assert first is second

    def test_get_checkpointer_before_init_returns_none(self):
        assert ckpt_module.get_checkpointer() is None


class TestLgBuilderBuildGraph:
    """build_graph 工厂：None / MemorySaver 均可编译，模块级默认图存在。"""

    def test_module_level_default_graph_exists(self):
        from agentix.orchestration.langgraph import lg_builder

        assert lg_builder.graph is not None
        assert lg_builder.checkpointer is not None

    def test_build_graph_with_memory_saver(self):
        from agentix.orchestration.langgraph import lg_builder

        g = lg_builder.build_graph(checkpointer=MemorySaver())
        assert g is not None
        # checkpointer 已接入
        assert g.checkpointer is not None

    def test_build_graph_without_checkpointer(self):
        from agentix.orchestration.langgraph import lg_builder

        g = lg_builder.build_graph(checkpointer=None)
        assert g is not None

    def test_build_graph_nodes_registered(self):
        from agentix.orchestration.langgraph import lg_builder

        g = lg_builder.build_graph(checkpointer=None)
        node_names = set(g.get_graph().nodes.keys())
        expected = {
            "analyze_and_route_query",
            "respond_to_general_query",
            "get_additional_info",
            "create_research_plan",
            "create_image_query",
            "create_file_query",
            "create_kb_query",
        }
        assert expected.issubset(node_names)

    def test_startup_override_pattern(self):
        """模拟 main.py startup 的覆盖模式：lg_builder.graph 属性访问能拿到新图。"""
        from agentix.orchestration.langgraph import lg_builder

        original = lg_builder.graph
        replacement = lg_builder.build_graph(checkpointer=MemorySaver())
        try:
            lg_builder.graph = replacement
            # 属性访问（chat.py 的方式）拿到的是覆盖后的图
            assert lg_builder.graph is replacement
        finally:
            lg_builder.graph = original
