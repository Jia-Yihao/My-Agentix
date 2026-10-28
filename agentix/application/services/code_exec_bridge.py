"""P2 代码执行工具的组合根装配（岗位对照·运行时写代码执行）。

职责切分（与 ``task_tools_bridge`` / ``notify_bridge`` 同款）：

- **声明**（spec + LLM schema）在 ``agentix/tools/adapters/code_exec.py``；
- **实现**（墙内运行时）在 ``agentix/code_exec_runtime.py``；
- 本模块只做接线：``register_handler``。

三条接线纪律：

1. **flag 关时直接返回**（``CODE_EXEC_ENABLED`` 缺省 false）⇒ 默认零注册、
   与基线逐位等价（纪律 1）。
2. **handler 必须是模块级函数**：墙内子进程按 ``module`` + ``qualname`` 重建
   handler（``agentix/tools/subprocess_worker.py`` 的 ``reconstruct``），闭包 /
   lambda / 绑定方法在另一个进程里不存在——所以这里**直接引用**
   ``agentix.code_exec_runtime.run_python_code``，**不包一层闭包**。包了会在
   ``sandbox.py::_describe_handler`` 被拒（``qualname`` 里带 ``<``），
   而那个拒绝来得比"跑不起来"更晚也更难懂。
3. **不进 LLM 可见的声明清单**：``registry_init._DECLARATIONS`` 本次不动
   （那是"模型看得见的工具"清单），``code_execute`` 只作为**执行点**上的工具
   存在——它由代码执行节点直接调用，不交给模型自由挑选。``register_handler``
   会把 spec 一并登记进工具注册表（执行点需要 spec 存在），但**声明清单**
   与基线逐项一致。
"""

from __future__ import annotations

from agentix.tools.adapters.code_exec import (
    CODE_EXEC_TOOL_NAME,
    build_code_execute_spec,
    code_exec_enabled,
)

__all__ = ["install_code_execute_tool"]


def install_code_execute_tool() -> None:
    """注册 ``code_execute`` 处理器（幂等；flag 关时零注册）。

    幂等性来自两处：同 spec 重复注册只是把同一个 handler 再绑一次（不产生第二份
    声明），而重复安装不会改变任何运行期状态——装配本身没有副作用。
    """
    if not code_exec_enabled():
        return  # belt-and-suspenders：调用方已判 flag，这里再判一次
    from agentix.code_exec_runtime import run_python_code
    from agentix.tools.gateway.executor import get_tool_executor

    executor = get_tool_executor()
    executor.register_handler(
        CODE_EXEC_TOOL_NAME,
        # 模块级函数（不是闭包）：墙内子进程按 module+qualname 重建它。
        run_python_code,
        spec=build_code_execute_spec(),
    )
