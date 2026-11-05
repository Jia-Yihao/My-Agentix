"""性能运行时基础设施：准入、预算、延迟拆解与负载甩脱。

W0 落地：延迟拆解（latency）+ 延迟预算（budget）。
后续波次按规划追加：admission（准入）、load_shedder（降级阶梯）等。
"""

from agentix.performance.budget import enforce as enforce_budget
from agentix.performance.budget import evaluate as evaluate_budget
from agentix.performance.latency import LatencyBreakdown, capture, current, phase

__all__ = [
    "LatencyBreakdown",
    "capture",
    "current",
    "phase",
    "enforce_budget",
    "evaluate_budget",
]
