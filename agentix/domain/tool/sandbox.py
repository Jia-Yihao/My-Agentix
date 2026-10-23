"""执行沙箱端口（§24.3 T2 ⑭，ADR-0007）。

域层只回答一件事：**一次工具调用「在哪儿跑」是可以声明的**。它不实现任何一种沙箱，
也不假装四档都能跑——能跑的集合由 infrastructure 的注册表说了算；声明了跑不了的
档位在执行点拿到 :class:`SandboxUnavailable` 并**拒绝执行**，而不是悄悄退回进程内
（ADR-0007 的 fail-closed 口径：安全声明不是提示）。

四档阶梯（每一档买到什么 / 买不到什么，完整取舍见 ADR-0007）：

- ``in_process`` —— 现状：在调用它的那个事件循环里 await。什么都不隔离。
- ``thread`` —— 换一条线程跑。买到「别占住事件循环」（§24.2 G1），买不到隔离：
  同进程、同 GIL、同一套环境变量与文件权限。
- ``subprocess`` —— 独立进程 + ``rlimit`` + 独立 uid + 精简环境。**T2 ⑮，未实现。**
- ``container`` —— 容器/microVM 级 + 网络默认拒绝 + 每次调用全新文件系统。**T3 ⑰，未实现。**

零第三方依赖，且**不 import** ``agentix`` 的其它层（ADR-0001 的依赖方向：端口在上层）。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Protocol, Tuple, runtime_checkable

__all__ = [
    "Handler",
    "SANDBOX_LADDER",
    "SANDBOX_DEFAULT",
    "SandboxUnavailable",
    "ExecutionSandbox",
]

#: 工具处理器签名：``handler(args: dict) -> Any``（同步或 async 均可）。
#: 定义在端口上而不是 executor 里——沙箱与 executor 必须对「交给它的是什么」有同一个口径。
Handler = Callable[[Dict[str, Any]], Any] | Callable[[Dict[str, Any]], Awaitable[Any]]

#: 隔离阶梯（**可声明**的词汇表）。顺序有意义：从左到右「坏了波及多远」递减。
SANDBOX_LADDER: Tuple[str, ...] = ("in_process", "thread", "subprocess", "container")

#: 出厂默认档 = 今天的执行方式。改这个默认值就是改全项目每条工具调用的语义。
SANDBOX_DEFAULT = "in_process"


class SandboxUnavailable(RuntimeError):
    """声明的隔离档位当前不可用。

    抛这个而不是退回 ``in_process``：调用方要的是「出事了别在这个进程里」，
    给它进程内执行等于把声明当成装饰，而且是一次**静默**的降级——比拒绝执行更危险。
    """


@runtime_checkable
class ExecutionSandbox(Protocol):
    """一次工具调用的执行位置。

    **不管超时**：截断仍由 ``ToolExecutor`` 外层的 ``pooled_wait_for`` 负责（§24.2 G1
    已说明「超时只在事件循环让出来时成立」）。这条边界是刻意的——如果每个沙箱自己
    管超时，「这条工具的 timeout_ms 到底生不生效」就又变成要读实现才知道的事。
    """

    kind: str

    async def run(self, handler: Handler, arguments: Dict[str, Any]) -> Any:
        """执行 handler 并返回其结果；故障按原样抛出，由 executor 统一落成 ToolResult。"""
        ...
