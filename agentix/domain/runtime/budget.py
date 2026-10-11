"""Budget 契约（M0 拆入自 ``runtime/state.py`` BudgetState + ``runtime/budget.py``）。

预算值对象与超限异常——预算记账/检查实现（``BudgetManager``）留在
``runtime/budget.py``，纯契约下沉 domain（零第三方依赖）。
"""

from dataclasses import dataclass

__all__ = [
    "BudgetState",
    "Budget",
    "BudgetExceededError",
]


@dataclass
class BudgetState:
    """预算快照（由 BudgetManager 产出，文档 §8 ``budget``）。"""

    max_steps: int = 0
    max_llm_calls: int = 0
    max_tool_calls: int = 0
    max_tokens: int = 0
    max_latency_ms: int = 0
    max_cost_usd: float = 0.0
    used_steps: int = 0
    used_llm_calls: int = 0
    used_tool_calls: int = 0
    used_tokens: int = 0
    elapsed_ms: int = 0
    used_cost_usd: float = 0.0


@dataclass
class Budget:
    """运行预算（默认值对齐文档 §11；0 = 该维度不限额）。"""

    max_steps: int = 20
    max_llm_calls: int = 15
    max_tool_calls: int = 20
    max_tokens: int = 30000
    max_latency_ms: int = 60000
    max_cost_usd: float = 0.5


class BudgetExceededError(Exception):
    """预算超限（含超限维度与用量快照，供上层决定 STOP/降级）。"""

    def __init__(self, dimension: str, used: float, limit: float):
        self.dimension = dimension
        self.used = used
        self.limit = limit
        super().__init__(f"budget exceeded: {dimension} used={used} limit={limit}")
