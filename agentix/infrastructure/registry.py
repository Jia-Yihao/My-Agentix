"""通用插件注册表（可插拔扩展机制的基础设施）。

设计目标（对应《My Agentix_架构优化方案》2.2 节「可插拔组件」与 [第4章]
「工具集裁剪与渐进式披露」）：

- **开闭原则**：新增一种 Provider（LLM / Embedding / Reranker）只需在对应
  注册表 `register` 一项，**不改动调用链**（调用方按名字解析）。
- **零依赖**：仅使用 stdlib，可被任意层（含 infrastructure 各子包）复用。
- **显式失败**：解析未注册的名字抛 ``KeyError``，绝不静默降级为默认实现
  （静默降级会让「配置写错」表现成「功能异常」，难以定位）。

与 ``infrastructure.tools.definitions.ToolRegistry`` 的关系：
``ToolRegistry`` 面向「LLM 工具 schema + 描述」，语义更专一（带 ``ToolSpec``）；
本模块是**通用**的 name → factory 容器。两者共存，各司其职——
工具请继续用 ``ToolRegistry``，Provider 类扩展点用本模块。
"""

from __future__ import annotations

from typing import Callable, Dict, Generic, Iterator, List, Optional, Tuple, TypeVar

__all__ = ["PluginRegistry", "RegistryError"]

T = TypeVar("T")


class RegistryError(KeyError):
    """解析未注册插件名时抛出（继承 KeyError 以兼容既有 ``except KeyError``）。"""

    def __init__(self, kind: str, name: str, available: List[str]) -> None:
        self.kind = kind
        self.name = name
        self.available = available
        super().__init__(
            f"未注册的 {kind} 插件: {name!r}（已注册: {', '.join(sorted(available)) or '无'}）"
        )


class PluginRegistry(Generic[T]):
    """按名字注册 / 解析插件工厂。

    泛型参数 ``T`` 是工厂产物的类型（如 ``ChatOpenAI``、``RerankerBackend``）。

    线程安全性：注册通常在 import 期完成（单线程），解析在请求期只读，
    故本实现不做加锁——与 ``ToolRegistry`` 保持一致。若需运行期动态注册，
    请自行在外层加锁。
    """

    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._factories: Dict[str, Callable[..., T]] = {}

    # -- 注册 ---------------------------------------------------------------

    def register(
        self,
        name: str,
        factory: Callable[..., T],
        *,
        override: bool = False,
    ) -> None:
        """注册一个插件工厂。

        Args:
            name: 插件名（大小写敏感；调用方负责归一化，如 ``.lower()``）。
            factory: 无参或带参的可调用对象，调用即返回插件实例。
            override: 是否允许覆盖已注册的同名插件。默认 ``False``——
                重复注册视为编程错误并抛 ``ValueError``，避免「后注册者
                静默覆盖前者」导致的排查困难。

        Raises:
            ValueError: 名字为空，或同名已注册且 ``override=False``。
        """
        if not name:
            raise ValueError(f"{self._kind} 插件名不能为空")
        if not override and name in self._factories:
            raise ValueError(
                f"{self._kind} 插件 {name!r} 已注册；如需覆盖请显式传 override=True"
            )
        self._factories[name] = factory

    def provider(self, name: str, *, override: bool = False) -> Callable[[Callable[..., T]], Callable[..., T]]:
        """装饰器形式的注册（便于在插件模块内就地声明）。"""

        def deco(fn: Callable[..., T]) -> Callable[..., T]:
            self.register(name, fn, override=override)
            return fn

        return deco

    def unregister(self, name: str) -> None:
        """移除已注册插件（不存在时静默返回；主要用于测试隔离）。"""
        self._factories.pop(name, None)

    def clear(self) -> None:
        """清空注册表（主要用于测试隔离）。"""
        self._factories.clear()

    # -- 解析 ---------------------------------------------------------------

    def has(self, name: str) -> bool:
        return name in self._factories

    def factory(self, name: str) -> Callable[..., T]:
        """返回工厂本身（不调用）。未注册抛 ``RegistryError``。"""
        try:
            return self._factories[name]
        except KeyError:
            raise RegistryError(self._kind, name, list(self._factories)) from None

    def get(self, name: str, *args, **kwargs) -> T:
        """解析并**调用**工厂，返回插件实例。未注册抛 ``RegistryError``。"""
        return self.factory(name)(*args, **kwargs)

    def maybe_get(self, name: str, *args, **kwargs) -> Optional[T]:
        """解析插件；未注册返回 ``None``（用于「可选增强」场景）。

        ⚠️ 仅在「缺省时确有合理降级语义」时使用；配置驱动的核心组件请用
        ``get`` 以 fail-fast。
        """
        if name not in self._factories:
            return None
        return self._factories[name](*args, **kwargs)

    # -- 内省 ---------------------------------------------------------------

    def names(self) -> List[str]:
        """已注册插件名（注册顺序）。"""
        return list(self._factories.keys())

    def items(self) -> Iterator[Tuple[str, Callable[..., T]]]:
        return iter(self._factories.items())

    def __contains__(self, name: object) -> bool:
        return name in self._factories

    def __len__(self) -> int:
        return len(self._factories)

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"PluginRegistry(kind={self._kind!r}, plugins={self.names()})"
