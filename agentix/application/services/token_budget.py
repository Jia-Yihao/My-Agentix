"""Token 预算：把「只计量」升级为「可限额、可降级」。

《My Agentix_架构优化方案》§9.1 D 组缺口：「硬约束最大轮数/并发/token 预算」
—— 并发 ✅、超时 ✅、轮数 ✅，**token 预算仍 ❌**（现状只统计 tokens 与估算
成本，没有任何上限）。

设计要点：

- **两级预算**：请求级（``per_request``）与 子 Agent 级（``per_agent``）。
  子 Agent 预算是请求预算的**切片**——防止某个子图吃掉整条请求的额度。
- **三档判定**（而非二值）：
  ``OK`` → 正常；``WARN`` → 已达预警线（默认 80%），可触发「收紧上下文」；
  ``EXCEEDED`` → 已达硬线（默认 100%），调用方必须降级（截断/拒答/收尾）。
- **纯逻辑、零 I/O**：便于单测，也便于被 ``chat_service`` 在任意检查点调用。
- **默认关闭**：``TOKEN_BUDGET_PER_REQUEST = 0`` 表示不限额，
  行为与接线前**逐位一致**（可安全灰度）。

与 ``request_scope`` 的分工：``request_scope`` 负责**计量与上报**（用了多少），
本模块负责**限额与判定**（还能用多少、超了怎么办）。两者互补，不重叠。

**M2-3 拆边（2026-10）**：纯数据结构 ``TokenBudget`` / ``BudgetVerdict`` /
``DEFAULT_WARN_RATIO`` 的规范位已下沉 ``agentix.domain.runtime.token_budget``
（口径见分层架构文档 §7.4——为拆 ``runtime → application`` 的类型引用边）；
本模块**再导出 shim 保持对象同一**，既有消费方 import 路径不变。
``BudgetLedger``、ContextVar 台账读写与 ``budget_exhausted_error`` 的
**写入/错误构造实现留在本层**（依赖 config 与 domain.errors，不下沉）。
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 再导出（对象同一）：消费方经本路径取 TokenBudget 等照旧可用。
from agentix.domain.runtime.token_budget import (  # noqa: F401 - re-export shim（规范位在 domain）
    DEFAULT_WARN_RATIO,
    BudgetVerdict,
    TokenBudget,
)

__all__ = [
    "BudgetVerdict",
    "TokenBudget",
    "BudgetLedger",
    "DEFAULT_WARN_RATIO",
    "set_current_ledger",
    "get_current_ledger",
    "clear_current_ledger",
    "budget_exhausted_error",
]


@dataclass
class BudgetLedger:
    """一条请求的预算台账：请求级总额度 + 各子 Agent 的切片。

    不变式：``sum(agent.limit) <= request.limit``（``_rebalance`` 保证）。
    子 Agent 未显式分配额度时，按 ``default_agent_limit`` 切分；
    请求不限额时，子 Agent 也不限额。
    """

    request: TokenBudget = field(default_factory=lambda: TokenBudget(name="request"))
    default_agent_limit: int = 0
    max_tool_calls: int = 0
    tool_calls: int = 0
    agents: Dict[str, TokenBudget] = field(default_factory=dict)

    # -- 构造 ---------------------------------------------------------------

    @classmethod
    def from_settings(
        cls, *, per_request: int, per_agent: int = 0, max_tool_calls: int = 0
    ) -> "BudgetLedger":
        return cls(
            request=TokenBudget(limit=int(per_request), name="request"),
            default_agent_limit=int(per_agent),
            max_tool_calls=max(0, int(max_tool_calls)),
        )

    @property
    def tool_budget_exhausted(self) -> bool:
        return self.max_tool_calls > 0 and self.tool_calls >= self.max_tool_calls

    def record_tool_call(self) -> bool:
        """记录一次工具调用；返回是否仍允许继续调用。"""
        self.tool_calls += 1
        return not self.tool_budget_exhausted

    # -- 子 Agent 预算 ------------------------------------------------------

    def for_agent(self, agent_name: str) -> TokenBudget:
        """取（或创建）某子 Agent 的预算单元。"""
        key = str(agent_name)
        existing = self.agents.get(key)
        if existing is not None:
            return existing

        if not self.request.enabled:
            # 请求不限额 ⇒ 子 Agent 也不限额（保持「默认关闭」语义）
            budget = TokenBudget(limit=0, name=key)
        else:
            limit = int(self.default_agent_limit) or self._fair_share()
            limit = max(1, min(limit, self.request.limit))
            budget = TokenBudget(limit=limit, name=key)
        self.agents[key] = budget
        return budget

    def _fair_share(self) -> int:
        """未配置 per-agent 时的均分额度：按「已有 + 1」等分请求额度。"""
        n = len(self.agents) + 1
        return max(1, int(self.request.limit) // n)

    # -- 汇总 ---------------------------------------------------------------

    def consume(self, tokens: int, *, agent: Optional[str] = None) -> None:
        """记入消耗；``agent`` 非空时**同时**记入请求级与子 Agent 级。"""
        self.request.consume(tokens)
        if agent:
            self.for_agent(agent).consume(tokens)

    def verdict(self, *, agent: Optional[str] = None) -> BudgetVerdict:
        """综合判定：请求级与（可选的）子 Agent 级取**更严**者。"""
        order = {BudgetVerdict.OK: 0, BudgetVerdict.WARN: 1, BudgetVerdict.EXCEEDED: 2}
        worst = self.request.verdict()
        if agent:
            candidate = self.for_agent(agent).verdict()
            if order[candidate] > order[worst]:
                worst = candidate
        return worst

    def agent_totals(self) -> int:
        return sum(b.used for b in self.agents.values())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "agents": {name: b.to_dict() for name, b in self.agents.items()},
            "tool_calls": self.tool_calls,
            "max_tool_calls": self.max_tool_calls,
            "tool_budget_exhausted": self.tool_budget_exhausted,
            "agent_total_used": self.agent_totals(),
            "unattributed_used": max(0, self.request.used - self.agent_totals()),
        }

    def snapshot_lines(self) -> List[str]:
        """可读的台账行（用于日志/诊断）。"""
        lines = [
            "[budget] request limit={} used={} remaining={} verdict={}".format(
                self.request.limit,
                self.request.used,
                self.request.remaining,
                self.request.verdict().value,
            )
        ]
        for name, budget in sorted(self.agents.items()):
            lines.append(
                "  [budget] agent={} limit={} used={} verdict={}".format(
                    name, budget.limit, budget.used, budget.verdict().value
                )
            )
        return lines


# ---------------------------------------------------------------------------
# 请求作用域内的当前台账（供 LLM 调用层做「先判断再调用」）
#
# 为什么需要：预算要在 **LLM 调用发生前** 生效（先判断再调用），
# 但 LLM 客户端是在别处构造的，拿不到请求级 ledger。用 ContextVar 传递与
# `request_scope` 完全同构——同一请求内的 asyncio 子任务可见父作用域的绑定。
# ---------------------------------------------------------------------------

_current_ledger: "ContextVar[Optional[BudgetLedger]]" = ContextVar(
    "agentix_current_budget_ledger", default=None
)


def set_current_ledger(ledger: Optional[BudgetLedger]) -> None:
    """绑定当前请求的预算台账（请求开始时调用）。"""
    _current_ledger.set(ledger)


def get_current_ledger() -> Optional[BudgetLedger]:
    """取当前请求的预算台账；未启用预算时为 ``None``。"""
    return _current_ledger.get()


def clear_current_ledger() -> None:
    """解绑（请求结束时调用，防止跨请求污染）。"""
    _current_ledger.set(None)


def budget_exhausted_error(agent: Optional[str] = None) -> Optional[Exception]:
    """若当前预算已用尽，返回应抛出的 ``AgentError``；否则返回 ``None``。

    这是「**先判断再调用**」的检查点：由 LLM 调用层在发起请求前调用，
    使 token 预算从"只计量"升级为"真限额"。

    预算未启用（ledger 为 None 或 limit<=0）时恒返回 ``None`` —— 零行为变化。
    """
    ledger = get_current_ledger()
    if ledger is None:
        return None
    if ledger.tool_budget_exhausted:
        from agentix.domain.errors import AgentError, ErrorCode

        return AgentError(
            ErrorCode.TOKEN_BUDGET_EXCEEDED,
            "本次请求的工具调用预算已用尽，无法继续生成回答。",
            detail=f"tool_calls={ledger.tool_calls} limit={ledger.max_tool_calls}",
        )
    if ledger.verdict(agent=agent) is not BudgetVerdict.EXCEEDED:
        return None

    from agentix.domain.errors import AgentError, ErrorCode

    return AgentError(
        ErrorCode.TOKEN_BUDGET_EXCEEDED,
        "本次请求的 token 预算已用尽，无法继续生成回答。",
        detail=f"limit={ledger.request.limit} used={ledger.request.used}",
    )
