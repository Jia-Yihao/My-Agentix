"""请求级指标累加器（P2-2：per-request token / tool / cost 聚合）。

与 ``observability.record_llm_call``（per-LLM-call 明细写 Redis）互补：
本模块把**同一次用户请求内**的所有 LLM 调用 / 工具调用累加成一个聚合值，
供 ``RequestMetrics`` 落地（``llm_calls`` / ``total_tokens`` / ``tools_used``
/ ``estimated_cost``）。

**并发正确性设计（关键）**：
``ContextVar`` 里存的是**一个可变对象** ``RequestScope`` 的引用，而不是若干个
``ContextVar[int]``。原因是 ``asyncio.gather`` / ``asyncio.create_task`` 会把
当前 context **浅拷贝**给子任务——拷贝的是「指向同一个对象的引用」：
- 子任务内 ``record_llm_usage()`` 修改的是**同一个** ``RequestScope`` 实例，
  父作用域能立刻看到累加结果；
- 若改用 ``ContextVar[int]`` 并 ``var.set(var.get() + 1)``，子任务拿到的是
  值的拷贝，累加只写在子任务的 context 里，父作用域**看不到**——数据丢失。

本文件的 ``tests/test_p22_obs_scope.py::TestGatherVisibility`` 用
``asyncio.gather`` 实测证明「子任务累加对父作用域可见」。

**M2-2 拆边（2026-10）**：纯类型 ``RequestScope`` / ``_scope_var`` /
``get_current_scope`` 的规范位已下沉 ``agentix.domain.request_scope``
（与 ``domain/tracing.py`` 同构，口径见分层架构文档 §7.4）；本模块改为
**再导出 shim（对象同一）**，既有消费方 import 路径不变。本层保留的是
**写入/副作用实现**：``request_scope()`` CM、``record_*``，以及
``set_remaining_provider(...)`` 的注册（application 把 deadline reader
注册进 domain 的既有设计，不下沉——挪了就是行为变化）。

依赖方向：本模块依赖 stdlib（``contextlib``/``typing``）与
domain 层（``domain.policies.retry``、``domain.request_scope``）；
domain 不得反向依赖 application。无活动 scope 时所有记录函数都是
**静默 no-op**，绝不抛异常。
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from agentix.domain.policies.retry import set_remaining_provider

# 再导出（对象同一）：消费方经本路径取 RequestScope/get_current_scope 照旧可用。
from agentix.domain.request_scope import (  # noqa: F401 - re-export shim（规范位在 domain）
    RequestScope,
    _scope_var,
    get_current_scope,
)


def _remaining_seconds_from_scope() -> Optional[float]:
    """把当前作用域的 deadline 换算成秒，供 domain 重试层读取（LG-3-1）。"""
    scope = _scope_var.get()
    if scope is None:
        return None
    remaining_ms = scope.remaining_ms()
    return None if remaining_ms is None else remaining_ms / 1000.0


# LG-3-1：domain 不得反向依赖 application（分层），故由**本层**注册读取函数；
# 三层退避的停等只读这一个来源，不各自去猜 deadline。
set_remaining_provider(_remaining_seconds_from_scope)


@asynccontextmanager
async def request_scope() -> AsyncIterator[RequestScope]:
    """进入一个新的请求作用域，退出时**必定 reset**（防止泄漏到下一次请求）。"""
    scope = RequestScope()
    token = _scope_var.set(scope)
    try:
        yield scope
    finally:
        _scope_var.reset(token)


def record_llm_usage(
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
) -> None:
    """把一次 LLM 用量累加进当前作用域；无活动作用域时静默 no-op。"""
    scope = _scope_var.get()
    if scope is None:
        return
    scope.add_llm_usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )


def record_tool(name: str) -> None:
    """把一个工具名累加进当前作用域；无活动作用域时静默 no-op。"""
    scope = _scope_var.get()
    if scope is None:
        return
    scope.add_tool(name)
