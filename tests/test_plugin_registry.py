"""可插拔扩展机制测试（PluginRegistry + Provider 注册表）。

覆盖：
- ``PluginRegistry`` 通用容器语义（注册 / 解析 / 覆盖 / 幂等 / 装饰器 / 内省）
- LLM Provider 注册表（默认注册、名字归一化、未注册 fail-fast、自定义 Provider 端到端）
- Embedding Provider 注册表（默认注册、未注册 fail-fast）
- Reranker Provider 注册表（内置四项、按名分派）

全部离线，不依赖任何外部服务。
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from agentix.infrastructure.registry import PluginRegistry, RegistryError


# ---------------------------------------------------------------------------
# 1. 通用 PluginRegistry
# ---------------------------------------------------------------------------


class TestPluginRegistry:
    def setup_method(self):
        self.reg: PluginRegistry = PluginRegistry("test")

    def test_register_and_get(self):
        self.reg.register("a", lambda: 42)
        assert self.reg.get("a") == 42
        assert self.reg.has("a")
        assert "a" in self.reg
        assert self.reg.names() == ["a"]
        assert len(self.reg) == 1

    def test_get_passes_constructor_args(self):
        self.reg.register("sum", lambda x, y=0: x + y)
        assert self.reg.get("sum", 3, y=4) == 7

    def test_factory_returns_callable_without_invoking(self):
        calls: List[int] = []

        def build():
            calls.append(1)
            return "built"

        self.reg.register("b", build)
        fn = self.reg.factory("b")
        assert calls == [], "factory() 不得调用构建函数"
        assert fn() == "built"
        assert calls == [1]

    def test_unknown_name_raises_registry_error_with_available(self):
        self.reg.register("known", lambda: 1)
        with pytest.raises(RegistryError) as exc:
            self.reg.get("unknown")
        assert "unknown" in str(exc.value)
        assert "known" in str(exc.value), "报错须提示可用项"
        assert exc.value.available == ["known"]

    def test_registry_error_is_keyerror(self):
        """RegistryError 继承 KeyError，兼容既有 ``except KeyError``。"""
        with pytest.raises(KeyError):
            self.reg.get("nope")

    def test_duplicate_registration_is_rejected(self):
        self.reg.register("dup", lambda: 1)
        with pytest.raises(ValueError, match="已注册"):
            self.reg.register("dup", lambda: 2)

    def test_override_allows_replacement(self):
        self.reg.register("dup", lambda: 1)
        self.reg.register("dup", lambda: 2, override=True)
        assert self.reg.get("dup") == 2

    def test_empty_name_rejected(self):
        with pytest.raises(ValueError, match="不能为空"):
            self.reg.register("", lambda: 1)

    def test_maybe_get_returns_none_when_absent(self):
        assert self.reg.maybe_get("missing") is None

    def test_maybe_get_invokes_when_present(self):
        self.reg.register("x", lambda: "v")
        assert self.reg.maybe_get("x") == "v"

    def test_unregister_and_clear(self):
        self.reg.register("a", lambda: 1)
        self.reg.unregister("a")
        assert not self.reg.has("a")
        self.reg.unregister("a")  # 幂等，不抛
        self.reg.register("b", lambda: 2)
        self.reg.clear()
        assert self.reg.names() == []

    def test_provider_decorator(self):
        @self.reg.provider("deco")
        def build() -> str:
            return "deco-built"

        assert self.reg.get("deco") == "deco-built"

    def test_items_iteration(self):
        self.reg.register("a", lambda: 1)
        self.reg.register("b", lambda: 2)
        assert [n for n, _ in self.reg.items()] == ["a", "b"]


# ---------------------------------------------------------------------------
# 2. LLM Provider 注册表
# ---------------------------------------------------------------------------


class TestLLMProviderRegistry:
    def test_resolve_name_variants(self):
        from agentix.infrastructure.llm.provider_registry import resolve_llm_provider_name

        assert resolve_llm_provider_name("OpenAI") == "openai"
        assert resolve_llm_provider_name("  VLLM  ") == "vllm"
        assert resolve_llm_provider_name("") == "openai"
        assert resolve_llm_provider_name(None) == "openai"
        assert resolve_llm_provider_name(123) == "openai"  # 非字符串 → 默认
        assert resolve_llm_provider_name("   ") == "openai"

    def test_default_provider_registered_after_ensure(self):
        from agentix.application.agents.llm_factory import (
            ensure_llm_providers_registered,
        )
        from agentix.infrastructure.llm.provider_registry import (
            available_llm_providers,
            llm_providers,
        )

        ensure_llm_providers_registered()
        assert "openai" in llm_providers
        assert "openai" in available_llm_providers()

    def test_unknown_provider_fails_fast(self, monkeypatch):
        """配置了未注册的 Provider 必须 fail-fast，不得静默退回 openai。"""
        import agentix.application.agents.llm_factory as factory
        from agentix.config import settings

        # OPENAI_API_KEY 是只读 property（读 LLM_API_KEY），故 patch 底层字段
        monkeypatch.setattr(settings, "LLM_API_KEY", "k", raising=False)
        with pytest.raises(RegistryError, match="not-a-provider"):
            factory.get_llm_client(provider="not-a-provider")

    def test_custom_provider_end_to_end(self, monkeypatch):
        """注册自定义 Provider 后，get_llm_client 应经其构建。"""
        import agentix.application.agents.llm_factory as factory
        from agentix.config import settings
        from agentix.infrastructure.llm.provider_registry import llm_providers

        captured: Dict[str, Any] = {}

        def build(profile, *, temperature, tags, timeout, max_retries):
            captured.update(
                role=profile.role,
                temperature=temperature,
                tags=tags,
                timeout=timeout,
                max_retries=max_retries,
            )
            return "custom-client"

        monkeypatch.setattr(settings, "LLM_API_KEY", "k", raising=False)
        llm_providers.register("unit-test-provider", build, override=True)
        try:
            client = factory.get_llm_client(role="router", provider="unit-test-provider")
            assert client == "custom-client"
            assert captured["role"] == "router"
            # 构建函数收到的是原始 tags（None），由 Provider 自行决定回退到
            # profile.tags —— 与内置 openai Provider 的契约一致。
            assert captured["tags"] is None
            assert captured["timeout"] is None
            assert captured["max_retries"] is None
        finally:
            llm_providers.unregister("unit-test-provider")
