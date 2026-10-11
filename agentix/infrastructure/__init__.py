"""
Infrastructure layer providing adapters for persistence, vector stores, and external systems.

可插拔扩展：``agentix.infrastructure.registry.PluginRegistry`` 是通用插件容器；
各 Provider 注册表见 ``llm/provider_registry.py``。
"""

from .registry import PluginRegistry, RegistryError

__all__ = ["core", "persistence", "tools", "PluginRegistry", "RegistryError"]

