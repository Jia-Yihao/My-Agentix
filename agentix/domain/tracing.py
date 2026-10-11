"""请求追踪上下文（P2-2 分层修复）。

为什么放在 ``domain`` 层：
``trace_id`` 是贯穿 HTTP → Agent → LLM 的**跨层业务标识**，application 与
infrastructure 两侧都需要读取它。若把它定义在 ``interfaces.http.middleware``
（实际历史位置），``application`` 就不得不 ``import agentix.interfaces``，
构成 2.2 节明令禁止的反向依赖。把它下沉到 ``domain``（零第三方依赖、纯
stdlib）后，依赖方向回到 ``interfaces/infrastructure → domain``。

这与 ``domain/policies/error_classification.py`` 的迁移同理：把跨层共享的
纯逻辑/纯词汇表放在最内层，上层按正确方向引用。

本模块只依赖标准库 ``contextvars``，不引入任何第三方包（domain 层硬约束）。
"""

import contextvars

# 默认值 "-" 表示「不在请求上下文内」（例如进程启动期/后台任务），
# 便于日志里直观区分「无追踪」与「有追踪」。
trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_id", default="-"
)


def get_trace_id() -> str:
    """返回当前上下文的 trace_id（无则返回默认值 ``"-"``）。"""
    return trace_id_var.get()
