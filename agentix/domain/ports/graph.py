"""Agent 图供给抽象（Protocol）。

P1-6 的 ``ChatService`` 通过 ``graph_getter`` **惰性**获取图，以规避 P0-1
「启动期 Redis checkpointer 覆盖模块级图」的值绑定陷阱。这里把该注入点
提升为领域层抽象，使 application 不再需要描述「图从哪来」。

零第三方依赖：返回类型用 ``Any`` 表达（领域层不 import langgraph）。
"""

from typing import Any, Protocol, runtime_checkable

__all__ = ["GraphProviderPort"]


@runtime_checkable
class GraphProviderPort(Protocol):
    """可调用对象：每次调用返回**当前**的已编译图（不是构造期快照）。"""

    def __call__(self) -> Any:
        """返回当前的 CompiledStateGraph（或兼容的 Runnable）。"""
        ...
