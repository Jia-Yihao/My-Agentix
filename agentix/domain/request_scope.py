"""请求作用域纯类型（M2-2 拆 `tools → application` 边：规范位下沉 domain）。

为什么放在 ``domain`` 层（M2-2 拆边口径）：
``RequestScope`` 是「纯 ContextVar 存一个可变对象」，与 ``domain/tracing.py``
的 ``trace_id_var`` 同构；能力层（``tools/gateway/executor.py``）与
``reliability`` 只**读**它即可拿到 run deadline，不必反向 import application。
**写入与注册实现留在 application**（``application/services/request_scope.py``，
再导出 shim 保持对象同一）——尤其是 ``set_remaining_provider(...)``：那是
application 把 deadline reader 注册进 ``domain.policies.retry`` 的既有设计，
不属于本模块（CHANGELOG 登记的口径）。

**并发正确性设计（随类型一起搬来，未变）**：
``ContextVar`` 里存的是**一个可变对象** ``RequestScope`` 的引用，而不是若干个
``ContextVar[int]``。原因是 ``asyncio.gather`` / ``asyncio.create_task`` 会把
当前 context **浅拷贝**给子任务——拷贝的是「指向同一个对象的引用」：
- 子任务内 ``add_llm_usage()`` 修改的是**同一个** ``RequestScope`` 实例，
  父作用域能立刻看到累加结果；
- 若改用 ``ContextVar[int]`` 并 ``var.set(var.get() + 1)``，子任务拿到的是
  值的拷贝，累加只写在子任务的 context 里，父作用域**看不到**——数据丢失。

依赖方向：domain 层硬约束——只依赖 stdlib（``contextvars``/``dataclasses``/
``time``/``typing``）。``retry_budget`` 字段保持引用型（``Optional[Any]``），
不在这里 import 任何上层类型（domain 不得依赖上层）。
"""

import contextvars
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class RequestScope:
    """一次请求内的可变累加器（跨 gather 子任务共享同一实例）。"""

    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    tools: List[str] = field(default_factory=list)
    # run 预算传播（跨 gather 子任务共享同一对象引用）。
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
