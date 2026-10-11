"""M-P1-1 记忆向量×关键词双路召回融合（纯策略，无 IO）。

设计出处：``docs/design/memory-optimization.md`` §5 M-P1-1——``recall`` 改为
"关键词 × 向量" 双路 + recency 衰减（沿用 ``MEMORY_RECENCY_HALF_LIFE_DAYS``）。
本模块只做确定性打分/融合（stdlib only），embedding 与向量比对由
``agentix/agent/memory/service.py``（进程内 provider）在 flag 门控下接入，
向量不可用时整体 fail-open 回关键词路径（验收："断向量服务仍可答"）。

放 ``domain/policies`` 的理由与 F1a ``rank_fusion`` 一致：给 agent 层与
application 层共用的纯函数一个无依赖落点，不新增跨层边。
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

__all__ = [
    "cosine_similarity",
    "recency_weight",
    "fuse_recall_scores",
]

#: 双路融合权重（关键词侧）。向量侧权重 = 1 - 关键词侧。刻意用常量而非
#: settings 键——TopK 已由 ``MEMORY_RECALL_LIMIT`` 可配，避免键面膨胀。
KEYWORD_WEIGHT = 0.5


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度，∈[0, 1] 语义（负值截 0）。

    维度不匹配或零向量 ⇒ 返回 0.0（fail-open：坏向量不炸召回）。
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    sim = float(dot / (na ** 0.5 * nb ** 0.5))
    return max(0.0, min(1.0, sim))


def recency_weight(
    stored_at: float | None,
    *,
    half_life_days: float,
    now: float | None = None,
) -> float:
    """时效衰减权重：``0.5 ** (age_days / half_life)``。

    - ``half_life_days <= 0`` ⇒ 1.0（关闭衰减 = 旧行为）；
    - ``stored_at`` 缺失/非法 ⇒ 1.0（历史条目不被惩罚到消失）；
    - 未来时间戳（时钟漂移）⇒ 按 1.0 处理。
    """
    if half_life_days <= 0 or stored_at is None:
        return 1.0
    ts = now if now is not None else time_now()
    age = ts - stored_at
    if age <= 0:
        return 1.0
    return float(0.5 ** (age / 86400.0 / half_life_days))


def time_now() -> float:
    """可被测试替换的时间源（``monkeypatch.setattr(memory_recall, 'time_now', ...)``）。"""
    import time

    return time.time()


def fuse_recall_scores(
    candidate_ids: Sequence[str],
    keyword_hits: Iterable[str],
    vector_scores: Mapping[str, float],
    recency_weights: Mapping[str, float],
    *,
    keyword_weight: float = KEYWORD_WEIGHT,
) -> List[Tuple[str, float]]:
    """双路融合打分：``(w·kw + (1-w)·vec) × recency``，降序返回。

    - ``candidate_ids``：候选顺序（首见序）。排序为稳定排序 ⇒ 平分时保持
      候选顺序（与仓内其它融合函数同纪律）；
    - 关键词命中 = 1.0，未命中 = 0.0；向量侧取 cosine（缺失记 0.0）；
    - recency 缺失记 1.0。
    """
    kw = set(keyword_hits)
    out: List[Tuple[str, float]] = []
    for cid in candidate_ids:
        vec = float(vector_scores.get(cid, 0.0))
        kw_part = 1.0 if cid in kw else 0.0
        rec = float(recency_weights.get(cid, 1.0))
        score = (keyword_weight * kw_part + (1.0 - keyword_weight) * vec) * rec
        out.append((cid, score))
    out.sort(key=lambda kv: kv[1], reverse=True)
    return out


def weights_for(
    items: Sequence[object],
    *,
    half_life_days: float,
    now: float | None = None,
) -> Dict[str, float]:
    """按条目 ``meta.stored_at`` 批量生成 recency 权重（鸭子类型取 id/时间）。"""
    out: Dict[str, float] = {}
    for it in items:
        iid = getattr(it, "item_id", None)
        if not iid:
            continue
        meta = getattr(it, "meta", None) or {}
        stored = meta.get("stored_at") if isinstance(meta, dict) else None
        out[iid] = recency_weight(
            float(stored) if stored is not None else None,
            half_life_days=half_life_days,
            now=now,
        )
    return out
