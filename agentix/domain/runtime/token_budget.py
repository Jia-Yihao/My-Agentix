"""Token 预算纯数据结构（M2-3 拆 `runtime → application` 边：规范位下沉 domain）。

为什么放在 ``domain/runtime/``（口径：`docs/architecture/分层架构与依赖规则.md` §7.4）：
``TokenBudget`` 是「纯逻辑、零 I/O」的预算值对象；``runtime/control_plane.py``
的 ``AgentRun.budget`` 注解需要它，此前只能经 ``TYPE_CHECKING`` 引用
application 模块——拆边后注解边指向 domain，Runtime 簇对 application 连
类型引用边都不再有。**台账写入与错误构造实现留在 application**
（``application/services/token_budget.py``，再导出 shim 保持对象同一）。

与既有 ``domain/runtime/budget.py`` 的边界（刻意不并进去）：那边是
``Budget``/``BudgetState``/``BudgetExceededError`` 另一套值对象（轮数/步数
预算），本模块是 token 预算——同一概念在本项目只有一种形态，两套就各留各处。

依赖方向：domain 层硬约束——只依赖 stdlib（``dataclasses``/``enum``/``typing``）。
``to_dict()`` 的字段集合与键顺序是响应面契约，逐字不得变。
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

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
