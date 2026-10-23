"""``ExecutionSandbox`` 端口的 infrastructure 实现（§24.3 T2 ⑭，ADR-0007）。

⚠️ 这一层只有**两档真的能跑**，而且它们都不构成隔离：

- :class:`InProcessSandbox` —— 与 §24.2 G1 之前的现状逐位相同（含
  ``TOOL_SYNC_OFFLOAD_ENABLED`` 的那条下放分支，全项目只有这一份实现）；
- :class:`ThreadSandbox` —— 无条件把 handler 丢进 ``asyncio.to_thread``。买到的是
  「别占住事件循环」，买不到的是「坏了波及多远」：同进程、同 GIL、同一套
  ``os.environ`` 与文件权限——所以它**不是**安全边界，只是资源边界的雏形。

``subprocess`` / ``container`` 是 T2 ⑮ / T3 ⑰ 的落点，**这里没有实现**，
:func:`resolve_sandbox` 对它们抛 :class:`~agentix.domain.tool.sandbox.SandboxUnavailable`。
「词汇表里有四档」与「注册表里只有两档」的差集必须**可见**：那正是 ADR-0007
要被推翻时用的接口。
"""

from __future__ import annotations

import asyncio

from agentix.config import settings
from agentix.domain.tool.sandbox import (
    SANDBOX_DEFAULT,
    SANDBOX_LADDER,
    ExecutionSandbox,
    Handler,
    SandboxUnavailable,
)

__all__ = [
    "SANDBOX_LADDER",
    "SandboxUnavailable",
    "invoke_in_process",
    "InProcessSandbox",
    "ThreadSandbox",
    "implemented_kinds",
    "resolve_sandbox",
]


async def invoke_in_process(handler: Handler, arguments: dict) -> object:
    """G1 的那条口径，全项目唯一一份（``ToolExecutor._invoke`` 也走这里）。

    同步 handler 在自己的栈帧里跑时事件循环被占住，外层 ``asyncio.wait_for``
    根本没有机会取消它——``timeout_ms`` 形同虚设。打开 ``TOOL_SYNC_OFFLOAD_ENABLED``
    后把它下放线程，循环空得出来，超时才真生效。
    """
    if settings.TOOL_SYNC_OFFLOAD_ENABLED and not asyncio.iscoroutinefunction(handler):
        outcome = await asyncio.to_thread(handler, arguments)
        if asyncio.iscoroutine(outcome):
            outcome = await outcome
        return outcome
    outcome = handler(arguments)
    if asyncio.iscoroutine(outcome):
        outcome = await outcome
    return outcome


class InProcessSandbox:
    """默认档：在调用它的那个事件循环里 await handler（= 今天的生产行为）。"""

    __slots__ = ()
    kind = SANDBOX_DEFAULT

    async def run(self, handler: Handler, arguments: dict) -> object:
        return await invoke_in_process(handler, arguments)


class ThreadSandbox:
    """``thread`` 档：非协程 handler 一律下放线程，协程 handler 就地 await。

    与 ``in_process`` + ``TOOL_SYNC_OFFLOAD_ENABLED=true`` 的区别只有一件事：
    下放不再由**全局 flag** 决定，而是由**这条工具的声明**决定。这正是阶梯里
    ``thread`` 那一档存在的意义——为「这个 handler 会占住循环」单独表态，
    而不是为了它把全实例的循环亲和语义都改掉。
    """

    __slots__ = ()
    kind = "thread"

    async def run(self, handler: Handler, arguments: dict) -> object:
        if asyncio.iscoroutinefunction(handler):
            return await handler(arguments)
        outcome = await asyncio.to_thread(handler, arguments)
        # 类形式的 async ``__call__`` 不是 iscoroutinefunction(实例)，但在 worker
        # 线程里造出来的协程对象仍然回到本循环 await——语义与 in_process 一致。
        if asyncio.iscoroutine(outcome):
            outcome = await outcome
        return outcome


#: 档位名 → 单例（无状态，进程级共享；``__slots__ = ()`` 保证它真的没有状态）
_SANDBOXES: dict[str, ExecutionSandbox] = {
    InProcessSandbox.kind: InProcessSandbox(),
    ThreadSandbox.kind: ThreadSandbox(),
}


def implemented_kinds() -> tuple[str, ...]:
    """当前**真能跑**的档位（与 ``SANDBOX_LADDER`` 的差集就是 T2 ⑮/⑰ 的余量）。"""
    return tuple(sorted(_SANDBOXES))


def resolve_sandbox(kind: str) -> ExecutionSandbox:
    """把声明的档位翻成实现；不可用就抛，**绝不静默退回 in_process**。

    两类不可用分开措辞，因为修法不同：``not_in_ladder`` 是拼写错/词汇表外，
    ``not_implemented`` 是声明了一个还没落地的隔离档位（此时该改声明，或等 ⑮）。
    """
    sandbox = _SANDBOXES.get(kind)
    if sandbox is not None:
        return sandbox
    if kind not in SANDBOX_LADDER:
        raise SandboxUnavailable(
            f"unknown execution sandbox kind {kind!r}: not in the ladder "
            f"({', '.join(SANDBOX_LADDER)})"
        )
    raise SandboxUnavailable(
        f"execution sandbox kind {kind!r} is in the ladder but not implemented "
        "(§24.3 T2 ⑮ / T3 ⑰; see ADR-0007); implemented kinds: "
        f"{', '.join(implemented_kinds())}"
    )
