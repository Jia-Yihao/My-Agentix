"""目标优先级与冲突裁决（纯领域规则，零外部依赖）。

《My Agentix_架构优化方案》§9.1 D 组缺口：「有目标优先级与冲突裁决规则」——
现状：无对应模块；``guardrails`` 仅做 scope 判定。

**为什么需要它**：Agent 同时被多个目标拉扯，而目标之间**天然冲突**：

- 「尽量帮忙」（helpfulness） vs 「越界必须拒答」（compliance）
- 「回答要快」（latency） vs 「引用必须可溯源」（accuracy）
- 「省 token」（cost） vs 「回答要完整」（helpfulness）

没有显式规则时，冲突会由**代码的偶然执行顺序**决定——同一类问题在不同
路径上得到不同处置，且无人能解释为什么。本模块把「谁让谁」写成**可枚举、
可测试、可评审**的规则。

**规则分两层**（先硬后软，避免「什么都按优先级排」的粗暴做法）：

1. **硬优先级**（``PRECEDENCE``）：安全 > 规范 > 准确 > 有用 > 时延 > 成本。
   高优先目标**否决**（veto）与之冲突的低优先目标。
2. **显式覆盖**（``Goal.priority``）：同一 ``GoalKind`` 的实例可用更小的
   ``priority`` 数值提权（例如「本请求必须最快」把 LATENCY 提到 0）。
   显式覆盖**只在同层内**生效，不得越过硬优先级——安全不可被配置让路。

**确定性**：所有排序都有完整 tie-break（优先级 → 显式 priority → 输入顺序），
同一输入永远得到同一输出（可复现，可写进快照测试）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "GoalKind",
    "Goal",
    "ConflictResolution",
    "ArbitrationResult",
    "PRECEDENCE",
    "CONFLICTING_PAIRS",
    "arbitrate",
    "dominant_goal",
]


class GoalKind(str, Enum):
    """目标种类（按硬优先级从高到低排列）。"""

    SAFETY = "safety"            # 不伤害、不越界
    COMPLIANCE = "compliance"    # 规范：该拒答就拒答、该免责就免责、必须引用
    ACCURACY = "accuracy"        # 事实正确、引用可溯源
    HELPFULNESS = "helpfulness"  # 有用、完整、可读
    LATENCY = "latency"          # 响应速度
    COST = "cost"                # token / 成本


#: 硬优先级：数值越小越优先（**不可被配置让路**）
PRECEDENCE: Dict[GoalKind, int] = {
    GoalKind.SAFETY: 0,
    GoalKind.COMPLIANCE: 1,
    GoalKind.ACCURACY: 2,
    GoalKind.HELPFULNESS: 3,
    GoalKind.LATENCY: 4,
    GoalKind.COST: 5,
}

#: 已知冲突对 → 裁定流程依据（人类可读，直接进审计日志/文档）
CONFLICTING_PAIRS: Dict[frozenset, str] = {
    frozenset({GoalKind.HELPFULNESS, GoalKind.COMPLIANCE}): (
        "越界问题必须拒答，即使「帮忙」看起来更有用（规范优先）"
    ),
    frozenset({GoalKind.HELPFULNESS, GoalKind.SAFETY}): (
        "任何「有用」都不得以安全为代价（安全优先）"
    ),
    frozenset({GoalKind.ACCURACY, GoalKind.LATENCY}): (
        "宁可慢一点也不要给错答案（准确优先）"
    ),
    frozenset({GoalKind.ACCURACY, GoalKind.COST}): (
        "不得为了省 token 牺牲引用可溯源性（准确优先）"
    ),
    frozenset({GoalKind.HELPFULNESS, GoalKind.COST}): (
        "回答完整性优先于成本（成本优先仅在同层内生效）"
    ),
    frozenset({GoalKind.HELPFULNESS, GoalKind.LATENCY}): (
        "回答质量优先于速度（速度优先需显式提权）"
    ),
    frozenset({GoalKind.ACCURACY, GoalKind.COMPLIANCE}): (
        "规范要求（如强制引用/免责）优先于一般准确性优化"
    ),
    frozenset({GoalKind.SAFETY, GoalKind.COMPLIANCE}): (
        "安全高于规范：规范操作若危及安全，安全否决"
    ),
}


@dataclass(frozen=True)
class Goal:
    """一个被追求的目标。

    :param kind: 目标种类（决定硬优先级）。
    :param priority: 显式提权值（越小越优先）；``None`` 表示用硬优先级。
        **只在同一 ``kind`` 层内生效**，不能越过硬优先级。
    :param note: 人类可读说明（进审计日志）。
    """

    kind: GoalKind
    priority: Optional[int] = None
    note: str = ""

    @property
    def effective_priority(self) -> int:
        base = PRECEDENCE[self.kind]
        return base if self.priority is None else min(base, int(self.priority))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "priority": self.priority,
            "effective_priority": self.effective_priority,
            "note": self.note,
        }


@dataclass
class ConflictResolution:
    """一次冲突的裁决记录。"""

    winner: Goal
    loser: Goal
    rule: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "winner": self.winner.kind.value,
            "loser": self.loser.kind.value,
            "rule": self.rule,
        }


@dataclass
class ArbitrationResult:
    """裁定流程结果。"""

    #: 按优先级从高到低排序的目标
    ordered: List[Goal] = field(default_factory=list)
    #: 被否决的目标（在冲突中落败）
    vetoed: List[Goal] = field(default_factory=list)
    #: 冲突裁决明细
    resolutions: List[ConflictResolution] = field(default_factory=list)
    #: 输入中未出现在冲突表里的目标对（提示「可能还没定义规则」）
    undefined_pairs: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def dominant(self) -> Optional[Goal]:
        return self.ordered[0] if self.ordered else None

    @property
    def has_conflicts(self) -> bool:
        return bool(self.resolutions)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ordered": [g.to_dict() for g in self.ordered],
            "vetoed": [g.to_dict() for g in self.vetoed],
            "resolutions": [r.to_dict() for r in self.resolutions],
            "undefined_pairs": [list(p) for p in self.undefined_pairs],
            "dominant": self.dominant.kind.value if self.dominant else None,
        }


def _pair_key(a: GoalKind, b: GoalKind) -> frozenset:
    return frozenset({a, b})


def arbitrate(goals: Sequence[Goal]) -> ArbitrationResult:
    """对一组目标做确定性排序 + 冲突裁决。

    排序键（全序，保证确定性）：``(effective_priority, 输入顺序)``。
    冲突裁决：对每一对**都在输入中**且**登记在冲突表**里的目标，
    高优先者胜出，低优先者记入 ``vetoed``。

    同 ``kind`` 的重复目标会被**去重**（保留 ``effective_priority`` 最小者；
    并列时保留先出现者）。
    """
    # 去重：同 kind 保留最优
    deduped: List[Goal] = []
    best: Dict[GoalKind, int] = {}
    for goal in goals:
        prev = best.get(goal.kind)
        if prev is None:
            best[goal.kind] = len(deduped)
            deduped.append(goal)
        elif goal.effective_priority < deduped[prev].effective_priority:
            deduped[prev] = goal

    ordered = sorted(
        deduped, key=lambda g: (g.effective_priority, PRECEDENCE[g.kind])
    )

    resolutions: List[ConflictResolution] = []
    vetoed: List[Goal] = []
    undefined: List[Tuple[str, str]] = []

    for i, first in enumerate(ordered):
        for second in ordered[i + 1 :]:
            key = _pair_key(first.kind, second.kind)
            rule = CONFLICTING_PAIRS.get(key)
            if rule is None:
                undefined.append((first.kind.value, second.kind.value))
                continue
            # ordered 已按优先级升序 ⇒ first 必然优先
            resolutions.append(ConflictResolution(winner=first, loser=second, rule=rule))
            if second not in vetoed:
                vetoed.append(second)

    return ArbitrationResult(
        ordered=ordered,
        vetoed=vetoed,
        resolutions=resolutions,
        undefined_pairs=undefined,
    )


def dominant_goal(goals: Sequence[Goal]) -> Optional[GoalKind]:
    """便捷函数：返回优先级最高的目标种类（空输入返回 ``None``）。"""
    result = arbitrate(goals)
    return result.dominant.kind if result.dominant else None
