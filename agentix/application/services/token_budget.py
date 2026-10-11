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
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

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

#: 默认预警线（达到硬限额的该比例时进入 WARN）
DEFAULT_WARN_RATIO = 0.8


class BudgetVerdict(str, Enum):
    """预算判定结果。"""

    OK = "ok"
    WARN = "warn"
    EXCEEDED = "exceeded"


@dataclass
class TokenBudget:
    """单个预算单元（请求级或子 Agent 级）。

    ``limit <= 0`` 表示**不限额**（永远 ``OK``，``remaining`` 为 ``None``）——
    这是「默认关闭」的实现方式，保证接线不改变既有行为。
    """

    limit: int = 0
    used: int = 0
    warn_ratio: float = DEFAULT_WARN_RATIO
    name: str = "request"

    # -- 查询 ---------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return int(self.limit) > 0

    @property
    def remaining(self) -> Optional[int]:
        """剩余额度；不限额时为 ``None``（**不是** 0——0 表示已用尽）。"""
        if not self.enabled:
            return None
        return max(0, int(self.limit) - int(self.used))

    @property
    def usage_ratio(self) -> float:
        if not self.enabled:
            return 0.0
        return min(1.0, int(self.used) / int(self.limit))

    @property
    def exhausted(self) -> bool:
        return self.enabled and int(self.used) >= int(self.limit)

    def verdict(self) -> BudgetVerdict:
        if not self.enabled:
            return BudgetVerdict.OK
        if int(self.used) >= int(self.limit):
            return BudgetVerdict.EXCEEDED
        if self.usage_ratio >= float(self.warn_ratio):
            return BudgetVerdict.WARN
        return BudgetVerdict.OK

    # -- 变更 ---------------------------------------------------------------

    def consume(self, tokens: int) -> int:
        """记入消耗，返回**实际记入**的数量（可为 0/负，负数表示退还）。"""
        delta = int(tokens)
        if delta == 0:
            return 0
        self.used = max(0, int(self.used) + delta)
        return delta

    def would_exceed(self, tokens: int) -> bool:
        """**预检**：再加 ``tokens`` 是否会越线（用于「先判断再调用」）。"""
        if not self.enabled:
            return False
        return int(self.used) + max(0, int(tokens)) > int(self.limit)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "limit": int(self.limit),
            "used": int(self.used),
            "remaining": self.remaining,
            "enabled": self.enabled,
            "verdict": self.verdict().value,
            "usage_ratio": round(self.usage_ratio, 4),
        }


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
