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

依赖方向：application 层模块，只依赖 stdlib（``contextvars``/``contextlib``）。
无活动 scope 时所有记录函数都是**静默 no-op**，绝不抛异常。
"""

import contextvars
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, List, Optional


@dataclass
class RequestScope:
    """一次请求内的可变累加器（跨 gather 子任务共享同一实例）。"""

    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    tools: List[str] = field(default_factory=list)
    # Phase H · W3：run 预算传播（跨 gather 子任务共享同一对象引用）。
    # deadline_at = time.monotonic() 绝对时刻；None = 未设（调用方回原行为）。
    deadline_at: Optional[float] = None
    # 共享重试预算桶（agentix.reliability.retry_budget.RetryBudget 实例；
    # 仅存引用，字段类型放宽以保持本模块只依赖 stdlib）。
    retry_budget: Optional[Any] = None

    def set_deadline_seconds(self, seconds: float) -> None:
        """设置本次请求的墙钟 deadline（monotonic；0/负 = 不设）。"""
        if seconds and seconds > 0:
            self.deadline_at = time.monotonic() + float(seconds)

    def remaining_ms(self) -> Optional[float]:
        """距 deadline 的剩余毫秒数；无 deadline 返回 None（调用方回原行为）。"""
        if self.deadline_at is None:
            return None
        return max(0.0, (self.deadline_at - time.monotonic()) * 1000.0)


    def add_llm_usage(
        self,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None:
        """累加一次 LLM 调用的用量（``total_tokens`` 缺省时按 prompt+completion 兜底）。"""
        self.llm_calls += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.total_tokens += (
            total_tokens if total_tokens else (prompt_tokens + completion_tokens)
        )

    def add_tool(self, name: str) -> None:
        """记录一个被调用的工具名（去重，保持首次出现的顺序）。"""
        if name and name not in self.tools:
            self.tools.append(name)

    def estimated_cost(
        self, *, input_price_per_1k: float, output_price_per_1k: float
    ) -> float:
        """按每 1K token 单价估算成本（单价默认 0.0 → 返回 0.0）。"""
        return (
            self.prompt_tokens / 1000.0
        ) * input_price_per_1k + (
            self.completion_tokens / 1000.0
        ) * output_price_per_1k


# 默认 None = 当前没有活动请求作用域（启动期/后台任务）。
_scope_var: contextvars.ContextVar[Optional[RequestScope]] = contextvars.ContextVar(
    "request_scope", default=None
)


def get_current_scope() -> Optional[RequestScope]:
    """返回当前活动的作用域（无则 None），供调用方填充 RequestMetrics。"""
    return _scope_var.get()


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
