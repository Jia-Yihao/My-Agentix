"""运行时策略（预算/风险上限）——Agent Core 侧轻量策略，不依赖 agentix.runtime。"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["LoopBudget"]


@dataclass(frozen=True)
class LoopBudget:
    """主循环预算快照（max_steps 为主循环硬限；其余为透传元数据）。"""

    max_steps: int = 20
    max_llm_calls: int = 15
    max_tool_calls: int = 20
    max_risk_level: str = "L1"

    def check_step(self, used: int) -> bool:
        """返回 True 表示还允许再走一步。"""
        if self.max_steps <= 0:
            return True
        return used < self.max_steps
