"""记忆整理（consolidation）与「可重建性」驱动的淘汰路径。

《My Agentix_架构优化方案》§9.1 C 组两条缺口：

* **有遗忘/整理机制** —— 原状态只有「TTL + 容量淘汰 + 检索时效加权」，
  缺**整理/合并**（consolidation）层面的时效驱动机制。
* **上下文压缩有「可重建性」判据** —— 判据（``is_rebuildable``）与溯源字段
  （``MemorySummary.source_message_ids``）已可用，但**只记录不阻断**，
  尚未接进淘汰路径。

本模块把两件事补齐，并**刻意把决策做成纯函数**：

1. :func:`select_consolidation_groups` —— 选出「同主题 + 时间相邻」的摘要组，
   供上层合并（时效驱动的整理）。
2. :func:`merge_summaries` —— 把一组摘要合并为一条（topics/key_facts 去重并集、
   ``source_message_ids`` 并集、时间取最早）。
3. :func:`plan_message_eviction` —— **把可重建判据接进淘汰路径**：
   待裁剪的原始消息只有在「已被某条摘要溯源覆盖」时才允许物理删除，
   否则一律保留（保守默认，避免不可逆信息损失）。
4. :func:`plan_summary_eviction` —— 摘要索引的淘汰计划（TTL + 容量）。

设计原则：

- **纯函数优先**：上述 4 个函数零 I/O、零外部依赖，可直接单测（离线）。
- **保守默认**：无证据（缺 id / 无摘要）⇒ **不淘汰**。吸取 evals「空真地板」教训：
  「没有证据说可以删」不等于「可以删」。
- **可观测**：计划对象带 ``unrebuildable_count`` / ``blocked`` 字段，
  便于判断覆盖率是否已足够支撑「只记录不阻断」升级为「阻断」。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from agentix.application.services.long_term_memory import (
    MemorySummary,
    is_rebuildable,
)

__all__ = [
    "EvictionPlan",
    "ConsolidationPlan",
    "select_consolidation_groups",
    "merge_summaries",
    "access_frequency_score",
    "recency_decay_score",
    "uniqueness_score",
    "importance_sort_key",
    "plan_message_eviction",
    "plan_summary_eviction",
    "summarize_coverage",
]

_DAY_SECONDS = 86400.0


@dataclass
class EvictionPlan:
    """原始消息淘汰计划（**可重建性已接入**）。"""

    #: 允许物理删除的消息（已被某条摘要溯源覆盖）
    drop: List[Dict[str, Any]] = field(default_factory=list)
    #: 必须保留的消息（无覆盖证明，或本身缺 id）
    retain: List[Dict[str, Any]] = field(default_factory=list)
    #: message_id -> 覆盖它的 summary.session_id
    covered_by: Dict[str, str] = field(default_factory=dict)
    #: 因「不可重建」而被拦下的条数（可观测：覆盖率不足时该值会很高）
    blocked: int = 0

    @property
    def dropped_count(self) -> int:
        return len(self.drop)

    @property
    def is_fully_covered(self) -> bool:
        return self.blocked == 0 and not self.retain


@dataclass
class ConsolidationPlan:
    """摘要整理计划。"""

    groups: List[List[MemorySummary]] = field(default_factory=list)
    #: 未参与合并的摘要（单独一条、或主题/时间不相邻）
    untouched: List[MemorySummary] = field(default_factory=list)

    @property
    def group_count(self) -> int:
        return len(self.groups)

    @property
    def merged_summary_count(self) -> int:
        """合并后减少的摘要条数（>0 表示确实发生了整理）。"""
        return sum(len(g) - 1 for g in self.groups)


# ---------------------------------------------------------------------------
# 1. 整理（consolidation）
# ---------------------------------------------------------------------------


def _summary_ts(summary: MemorySummary) -> float:
    return float(getattr(summary, "created_at", 0.0) or 0.0)


def select_consolidation_groups(
    summaries: Sequence[MemorySummary],
    *,
    now: Optional[float] = None,
    max_gap_days: float = 7.0,
    min_group_size: int = 2,
    max_groups: Optional[int] = None,
) -> ConsolidationPlan:
    """选出可合并的摘要组：**共享主题** 且 **时间相邻**。

    规则（确定性，便于测试与复现）：

    1. 按 ``created_at`` 升序排列；
    2. 对每个尚未归组的摘要，以它的第一个主题为「锚主题」，向后收集
       **共享该主题** 且与「组内上一条」间隔 ≤ ``max_gap_days`` 的摘要；
    3. 组内条数 < ``min_group_size`` 的不成组（单条合并无意义）。

    :param max_gap_days: 时间相邻阈值（天）；``<=0`` 表示**不做时间约束**
        （只按主题分组）。
    """
    ordered = sorted(summaries, key=lambda s: (_summary_ts(s), s.session_id))
    used: set = set()
    groups: List[List[MemorySummary]] = []

    for idx, anchor in enumerate(ordered):
        if anchor.session_id in used or not anchor.topics:
            continue
        anchor_topic = anchor.topics[0]
        group: List[MemorySummary] = [anchor]
        last_ts = _summary_ts(anchor)
        for later in ordered[idx + 1 :]:
            if later.session_id in used or anchor_topic not in later.topics:
                continue
            if max_gap_days > 0:
                gap_days = (_summary_ts(later) - last_ts) / _DAY_SECONDS
                if gap_days > max_gap_days:
                    break  # 时间不连续：终止本组（有序遍历，后续只会更远）
            group.append(later)
            last_ts = _summary_ts(later)

        if len(group) >= max(2, int(min_group_size)):
            for mem in group:
                used.add(mem.session_id)
            groups.append(group)
            if max_groups is not None and len(groups) >= max(1, int(max_groups)):
                break

    untouched = [s for s in ordered if s.session_id not in used]
    return ConsolidationPlan(groups=groups, untouched=untouched)


def merge_summaries(
    group: Sequence[MemorySummary],
) -> MemorySummary:
    """把一组摘要合并成一条（**纯函数**，不落库）。

    合并语义（全部为**并集**，不丢弃任何溯源信息）：

    - ``topics`` / ``key_facts``：按出现顺序去重合并；
    - ``source_message_ids``：并集——**合并后仍能证明覆盖了所有原始消息**，
      这是可重建判据在整理后继续成立的前提；
    - ``message_count`` / ``access_count``：求和；
    - ``created_at``：取**最早**（保守：整理不该让记忆「变新」）；
    - ``summary``：按时间顺序拼接各段（**不做 LLM 再摘要**——避免引入
      新的不可逆变换，使整理本身保持可审计）。
    """
    if not group:
        raise ValueError("merge_summaries 需要至少一条摘要")

    ordered = sorted(group, key=lambda s: (_summary_ts(s), s.session_id))
    head = ordered[0]

    topics: List[str] = []
    key_facts: List[str] = []
    source_ids: List[str] = []
    seen_topics: set = set()
    seen_facts: set = set()
    seen_ids: set = set()

    for mem in ordered:
        for topic in mem.topics:
            if topic not in seen_topics:
                seen_topics.add(topic)
                topics.append(topic)
        for fact in mem.key_facts:
            if fact not in seen_facts:
                seen_facts.add(fact)
                key_facts.append(fact)
        for mid in mem.source_message_ids:
            if mid not in seen_ids:
                seen_ids.add(mid)
                source_ids.append(mid)

    return MemorySummary(
        session_id=head.session_id,
        user_id=head.user_id,
        summary="\n".join(m.summary for m in ordered if m.summary),
        topics=topics,
        key_facts=key_facts,
        message_count=sum(m.message_count for m in ordered),
        created_at=min(_summary_ts(m) for m in ordered),
        access_count=sum(m.access_count for m in ordered),
        last_accessed=max(float(getattr(m, "last_accessed", 0.0) or 0.0) for m in ordered),
        source_message_ids=source_ids,
    )


# ---------------------------------------------------------------------------
# 1.5 重要性因子（Phase 24：书 §3 清单对照——访问频率 / 时间衰减 / 独特性）
# ---------------------------------------------------------------------------


def access_frequency_score(mem: MemorySummary) -> float:
    """访问频率因子：累积访问次数（越大越重要；缺失/负值按 0）。"""
    return float(max(0, int(mem.access_count)))


def recency_decay_score(
    mem: MemorySummary,
    *,
    now: Optional[float] = None,
    half_life_days: float = 30.0,
) -> float:
    """时间衰减因子：距 ``last_accessed`` 越久分越低，指数半衰 ∈ (0, 1]。

    ``half_life_days <= 0`` 视为不衰减（恒 1.0）；从未被访问过（
    ``last_accessed <= 0``）记 0.0。
    """
    ts_now = float(now if now is not None else time.time())
    last = float(getattr(mem, "last_accessed", 0.0) or 0.0)
    if last <= 0.0:
        return 0.0
    if half_life_days <= 0:
        return 1.0
    age_days = max(0.0, (ts_now - last) / _DAY_SECONDS)
    return 0.5 ** (age_days / half_life_days)


def uniqueness_score(
    mem: MemorySummary,
    topic_counts: Dict[str, int],
) -> float:
    """独特性因子：主题在语料中越稀有分越高 ∈ [0, 1]；无主题记 0。

    稀有定义为该主题在全部摘要中出现次数 ≤ 1（常见主题的记忆被淘汰时
    信息损失更小——相同信息可从其他同主题摘要重建）。
    """
    if not mem.topics:
        return 0.0
    rare = sum(1 for topic in mem.topics if topic_counts.get(topic, 0) <= 1)
    return rare / len(mem.topics)


def importance_sort_key(
    mem: MemorySummary,
    *,
    now: Optional[float] = None,
    topic_counts: Optional[Dict[str, int]] = None,
    half_life_days: float = 30.0,
) -> Tuple[float, float, float]:
    """重要性排序键（字典序）：**(访问频率, 时间衰减, 独特性)** 升序。

    与历史语义严格兼容：访问频率仍是第一主键、时间新鲜度仍是第二键
    （衰减单调于 ``last_accessed``，与原 ``(access_count, last_accessed)``
    同序），独特性仅作第三级 tie-break——决定「先淘汰谁」时，同等被访问
    次数与新鲜度下，主题更常见的记忆先走。
    """
    counts = topic_counts if topic_counts is not None else {}
    return (
        access_frequency_score(mem),
        recency_decay_score(mem, now=now, half_life_days=half_life_days),
        uniqueness_score(mem, counts),
    )


def _topic_counts(summaries: Sequence[MemorySummary]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for mem in summaries:
        for topic in mem.topics:
            counts[topic] = counts.get(topic, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# 2. 淘汰路径（可重建判据已接入）
# ---------------------------------------------------------------------------


def plan_message_eviction(
    messages: Sequence[Dict[str, Any]],
    summaries: Sequence[MemorySummary],
    *,
    keep_last_n: int = 20,
) -> EvictionPlan:
    """规划「原始消息」的物理淘汰，**受可重建判据保护**。

    待裁剪区 = ``messages[:-keep_last_n]``（最近 ``keep_last_n`` 条永不动）。
    对每条待裁剪消息：

    - 若能找到一条摘要，其 ``source_message_ids`` **完整覆盖**该消息的 id
      （复用 :func:`is_rebuildable`，含空真防护）⇒ 记入 ``drop``；
    - 否则 ⇒ 记入 ``retain``，并累加 ``blocked``。

    这即是「把判据接进淘汰路径」：**没有覆盖证明就不删**。
    """
    keep_n = max(0, int(keep_last_n))
    tail = list(messages[len(messages) - keep_n :]) if keep_n else []
    candidates = list(messages[: len(messages) - keep_n]) if keep_n else list(messages)

    plan = EvictionPlan()
    for msg in candidates:
        covering = _find_covering_summary(msg, summaries)
        if covering is None:
            plan.retain.append(msg)
            plan.blocked += 1
            continue
        plan.drop.append(msg)
        mid = msg.get("id") if isinstance(msg, dict) else None
        if mid is not None:
            plan.covered_by[str(mid)] = covering.session_id

    # 保底：tail 永不进入淘汰集（顺序无关的显式保证）
    tail_ids = {id(m) for m in tail}
    plan.drop = [m for m in plan.drop if id(m) not in tail_ids]
    return plan


def _find_covering_summary(
    message: Dict[str, Any], summaries: Sequence[MemorySummary]
) -> Optional[MemorySummary]:
    """找一条**能证明覆盖**该消息的摘要；找不到返回 None。"""
    for summary in summaries:
        if is_rebuildable([message], summary):
            return summary
    return None


def plan_summary_eviction(
    summaries: Sequence[MemorySummary],
    *,
    now: Optional[float] = None,
    ttl_days: float = 90.0,
    max_items: int = 200,
    min_access_count: int = 0,
    half_life_days: float = 30.0,
) -> Tuple[List[MemorySummary], List[MemorySummary]]:
    """摘要索引的淘汰计划：先过期、再超容（**保留访问过的**）。

    返回 ``(keep, evict)``。

    - **过期**：``age > ttl_days`` 且 ``access_count <= min_access_count``
      （被真正用过的记忆不因时间被丢）。
    - **超容**：剩余超出 ``max_items`` 时，按 :func:`importance_sort_key`
      升序淘汰——最少访问、最久未用、主题最常见者先走（访问频率仍为第一
      主键，与历史语义兼容；独特性仅作 tie-break）。
    - ``ttl_days <= 0`` 关闭过期淘汰；``max_items <= 0`` 关闭容量淘汰。
    """
    ts_now = float(now if now is not None else time.time())
    evict: List[MemorySummary] = []
    keep: List[MemorySummary] = []

    for mem in summaries:
        age_days = (ts_now - _summary_ts(mem)) / _DAY_SECONDS
        if ttl_days > 0 and age_days > ttl_days and mem.access_count <= min_access_count:
            evict.append(mem)
        else:
            keep.append(mem)

    if max_items > 0 and len(keep) > max_items:
        counts = _topic_counts(summaries)
        keep.sort(
            key=lambda m: importance_sort_key(
                m, now=ts_now, topic_counts=counts, half_life_days=half_life_days
            )
        )
        overflow = len(keep) - max_items
        evict.extend(keep[:overflow])
        keep = keep[overflow:]

    return keep, evict


def summarize_coverage(messages: Iterable[Dict[str, Any]], plan: EvictionPlan) -> Dict[str, Any]:
    """把淘汰计划压成可上报的覆盖率摘要（便于决定是否升级为「阻断」）。"""
    total = len(plan.drop) + len(plan.retain)
    covered = len(plan.drop)
    return {
        "total_candidates": total,
        "covered": covered,
        "blocked": plan.blocked,
        "coverage_ratio": round(covered / total, 4) if total else 0.0,
    }
