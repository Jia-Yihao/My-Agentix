"""LLM Provider 注册表（可插拔扩展点）。

设计依据（《My Agentix_架构优化方案》2.2 节「可插拔组件」/ [第4章]「渐进式披露」）：
把「用哪家 LLM」从硬编码的 ``ChatOpenAI(...)`` 提升为**按名解析的注册表**，
新增 Provider（如 anthropic / qwen / vllm）只需在对应模块 ``register`` 一项，
``get_llm_client`` 调用链零改动。

本模块是**纯容器**（零第三方依赖），不 import langchain / openai——
具体工厂由 ``application/agents/llm_factory.py`` 在首次调用时注册（懒加载），
避免基础设施层反向依赖应用层。

使用方式：

```python
from agentix.infrastructure.llm.provider_registry import llm_providers

# 1) 注册（通常在插件模块 import 期或 ensure_* 中）
llm_providers.register("my-provider", build_my_client)

# 2) 解析（调用方按 settings.LLM_PROVIDER 选择）
factory = llm_providers.factory("my-provider")
client = factory(profile=..., temperature=0.0, ...)
```

未注册的名字解析时抛 :class:`~agentix.infrastructure.registry.RegistryError`
（**fail-fast**，不静默退回默认实现）。
"""

from __future__ import annotations

from typing import Callable, List

from agentix.infrastructure.registry import PluginRegistry

__all__ = ["llm_providers", "available_llm_providers", "DEFAULT_LLM_PROVIDER"]

#: 内置默认 Provider（未显式配置 ``LLM_PROVIDER`` 时使用）
DEFAULT_LLM_PROVIDER = "openai"

#: LLM Provider 注册表：name → 构建函数
#: 构建函数签名约定：``(profile, *, temperature, tags, timeout, max_retries) -> ChatModel``
llm_providers: PluginRegistry = PluginRegistry("LLM provider")


def available_llm_providers() -> List[str]:
    """返回当前已注册的 LLM Provider 名（供 ``/ops`` 自省与报错提示）。"""
    return llm_providers.names()


def resolve_llm_provider_name(configured: object) -> str:
    """把配置值归一化为可用的 Provider 名。

    - 非字符串 / 空串 / 纯空白 → 回退 :data:`DEFAULT_LLM_PROVIDER`
      （配置缺失是常见情况，不应导致启动失败）；
    - 其余情况 → ``strip().lower()``。

    注意：**不在此处做「是否已注册」的校验**——校验交给注册表的
    ``factory()``，让「配置了但未注册」与「根本没配置」两种失败可区分。
    """
    if not isinstance(configured, str) or not configured.strip():
        return DEFAULT_LLM_PROVIDER
    return configured.strip().lower()


#: 构建函数类型别名（仅用于文档/类型标注，运行时不强制）
LLMProviderFactory = Callable[..., object]
