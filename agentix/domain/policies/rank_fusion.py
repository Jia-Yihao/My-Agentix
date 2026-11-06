"""R-P1-2 跨通道 rank 融合（纯函数，零第三方依赖）。

该模块保留两个消费场景：

- **主 RAG 检索链**（``agentix/retrieval/usecases/retrieval_hybrid.py``）：Dense + BM25
  的 z-score 融合；
- **知识图谱 local_search**：postgres 结构化路 + milvus 向量路的 RRF 融合，
  替代原先「postgres 非空则丢掉 milvus」的丢分逻辑
  （``.../multi_agent/kb_nodes/local_search_node.py``）。

放 domain 层的原因：application 与 retrieval 两侧都要用同一份融合算法，
放任一侧都会给另一侧新增跨层引用（application → retrieval 受
``tests/test_p74_m5_retrieval_unification.py`` 的 ``RETRIEVAL_BYPASS_ALLOW``
清单约束）；``domain/policies/`` 已有 ``legal_effectiveness`` 等实现型纯逻辑先例。

约定：入参分数方向一律「**越大越好**」（距离口径由调用方换向后再传入）；
并列分数按首次出现顺序稳定排序（确定性可回放）。
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Sequence, Tuple

__all__ = ["rrf_fuse", "zscore_fuse"]


def rrf_fuse(
    ranked_ids: Sequence[Iterable[str]],
    *,
    k: int = 60,
    top_n: int | None = None,
) -> List[Tuple[str, float]]:
    """Reciprocal Rank Fusion：``score(d) = Σ_i 1 / (k + rank_i(d))``（rank 从 1 起）。

    - ``k`` 越大各通道排名差异越平滑（仅知识图谱 local_search 使用 RRF）；
    - 同一通道内重复 id 只取首次出现；跨通道同 id 自动合并累加；
    - 并列分数保持首次出现顺序（稳定排序）；
    - ``top_n > 0`` 时截断。
    """
    eff_k = max(1, int(k))
    scores: Dict[str, float] = {}
    order: List[str] = []
    for ids in ranked_ids or []:
        if not ids:
            continue
        seen: set = set()
        for rank, doc_id in enumerate(ids, start=1):
            if doc_id is None or doc_id == "" or doc_id in seen:
                continue
            seen.add(doc_id)
            if doc_id not in scores:
                scores[doc_id] = 0.0
                order.append(doc_id)
            scores[doc_id] += 1.0 / (eff_k + rank)
    ranked = sorted(order, key=lambda doc_id: -scores[doc_id])
    result = [(doc_id, scores[doc_id]) for doc_id in ranked]
    if top_n is not None and top_n > 0:
        result = result[:top_n]
    return result


def zscore_fuse(
    channels: Sequence[Sequence[Tuple[str, float]]],
    *,
    alpha: float = 0.5,
    top_n: int | None = None,
) -> List[Tuple[str, float]]:
    """分通道 z-score 归一后加权求和。

    - 首个通道权重 ``alpha``，其余通道平分 ``1 - alpha``（两通道即
      dense=alpha、sparse=1-alpha；``alpha`` 截断到 [0, 1]）；
    - 每通道独立归一：``z = (s - mean) / std``；标准差为 0（单元素/同分）
      ⇒ 该通道全部记 0（不做除零近似，避免制造假区分度）；
    - 同通道重复 id 取首次；跨通道同 id 按加权和合并；
    - 并列分数保持首次出现顺序；``top_n > 0`` 时截断。
    """
    chan_list = [list(ch or ()) for ch in (channels or ())]
    n_ch = len(chan_list)
    if n_ch == 0:
        return []
    eff_alpha = min(1.0, max(0.0, float(alpha)))
    weights: List[float] = []
    if n_ch == 1:
        weights.append(1.0)
    else:
        weights.append(eff_alpha)
        rest = (1.0 - eff_alpha) / (n_ch - 1)
        weights.extend([rest] * (n_ch - 1))

    scores: Dict[str, float] = {}
    order: List[str] = []
    for weight, channel in zip(weights, chan_list):
        if not channel or weight == 0.0:
            # 权重 0 的通道不参与累加，但仍要保持其 id 不被凭空引入
            continue
        values = [float(s) for _, s in channel]
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / len(values)
        std = var ** 0.5
        for doc_id, raw in channel:
            if doc_id is None or doc_id == "":
                continue
            if doc_id not in scores:
                scores[doc_id] = 0.0
                order.append(doc_id)
            z = 0.0 if std <= 0 else (float(raw) - mean) / std
            scores[doc_id] += weight * z
    ranked = sorted(order, key=lambda doc_id: -scores[doc_id])
    result = [(doc_id, scores[doc_id]) for doc_id in ranked]
    if top_n is not None and top_n > 0:
        result = result[:top_n]
    return result
