"""Budget / BudgetManager——防无限循环（文档 §11）。

Agent 最大的问题之一是 Planner↔Tool 无限循环，Runtime 必须在每次执行前
``check``，超预算立即 STOP（文档 §11）。P0 维度：steps / llm_calls /
tool_calls / tokens / latency / cost。

与既有 ``application/services/token_budget.py`` 的关系：BudgetLedger 是
**编排域**的请求级 token 台账（子 Agent 切片语义），保留不动；BudgetManager
是 **Runtime 层**的全维度预算（六维：``steps`` / ``llm_calls`` / ``tool_calls`` /
``tokens`` / ``latency_ms`` / ``cost_usd``），面向 AgentState。二者归属层级不同，
接入点也不同。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from agentix.domain.runtime.budget import Budget, BudgetExceededError, BudgetState


@dataclass
class BudgetManager:
    """预算台账 + 执行前检查。所有 ``record_*`` 先记账后校验，超限抛
    :class:`BudgetExceededError`。"""

    budget: Budget = field(default_factory=Budget)
    used_steps: int = 0
    used_llm_calls: int = 0
    used_tool_calls: int = 0
    used_tokens: int = 0
    used_cost_usd: float = 0.0
    _t0: float = field(default_factory=time.perf_counter)

    # -- 执行前检查 -------------------------------------------------------

    def check_step(self) -> None:
        """计划新增一步前调用（已用步数 vs max_steps）。"""
        if self.budget.max_steps > 0 and self.used_steps >= self.budget.max_steps:
            raise BudgetExceededError("steps", self.used_steps, self.budget.max_steps)

    def check_tool_call(self) -> None:
        """计划发起一次工具调用前调用。"""
        if self.budget.max_tool_calls > 0 and self.used_tool_calls >= self.budget.max_tool_calls:
            raise BudgetExceededError("tool_calls", self.used_tool_calls, self.budget.max_tool_calls)

    def check_llm_call(self, tokens: int = 0) -> None:
        """计划发起一次 LLM 调用前调用（步数维度即判，token 量后置记账）。"""
        if self.budget.max_llm_calls > 0 and self.used_llm_calls >= self.budget.max_llm_calls:
            raise BudgetExceededError("llm_calls", self.used_llm_calls, self.budget.max_llm_calls)

    def check_latency(self) -> None:
        """整轮耗时检查（挂钟时间）。"""
        if self.budget.max_latency_ms > 0 and self.elapsed_ms() >= self.budget.max_latency_ms:
            raise BudgetExceededError("latency_ms", self.elapsed_ms(), self.budget.max_latency_ms)

    # -- 记账 -----------------------------------------------------------

    def record_step(self, n: int = 1) -> None:
        self.used_steps += n

    def record_tool_call(self, n: int = 1) -> None:
        self.used_tool_calls += n

    def record_llm_call(self, tokens: int = 0, cost_usd: float = 0.0) -> None:
        """记账一次 LLM 调用（token/成本后置累加并校验量维度）。"""
        self.used_llm_calls += 1
        self.used_tokens += max(0, tokens)
        self.used_cost_usd += max(0.0, cost_usd)
        if self.budget.max_tokens > 0 and self.used_tokens > self.budget.max_tokens:
            raise BudgetExceededError("tokens", self.used_tokens, self.budget.max_tokens)
        if self.budget.max_cost_usd > 0 and self.used_cost_usd > self.budget.max_cost_usd:
            raise BudgetExceededError("cost_usd", self.used_cost_usd, self.budget.max_cost_usd)

    # -- 快照 -----------------------------------------------------------

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._t0) * 1000)

    def snapshot(self) -> BudgetState:
        """产出 :class:`BudgetState` 快照（写回 AgentState.budget）。"""
        return BudgetState(
            max_steps=self.budget.max_steps,
            max_llm_calls=self.budget.max_llm_calls,
            max_tool_calls=self.budget.max_tool_calls,
            max_tokens=self.budget.max_tokens,
            max_latency_ms=self.budget.max_latency_ms,
            max_cost_usd=self.budget.max_cost_usd,
            used_steps=self.used_steps,
            used_llm_calls=self.used_llm_calls,
            used_tool_calls=self.used_tool_calls,
            used_tokens=self.used_tokens,
            elapsed_ms=self.elapsed_ms(),
            used_cost_usd=self.used_cost_usd,
        )
