"""Query 侧相关性记忆（默认关，flag-off 逐位等价）。

离线把「历史问法向量 → 相关文档 id」与「文档向量快照」打包成一个 npz；在线检索时
用当前 query 向量找近邻历史问法，得到两类补充信号：

1. **kNN 投票**：近邻历史问法的相关文档按 q-q 相似度加权计票；
2. **向量级 Rocchio**：近邻历史问法的相关文档向量质心回注到 query 向量，
   用它对候选文档重新打相似度分。

两者与原 dense 分数线性组合后重排候选池。纯函数、零第三方服务依赖；
服务/记忆文件不可用时由调用方 fail-open 保留原召回顺序。

与 STARD 评测口径的关系：`evals/stard_recall_lab.py` 的 ``blend`` 方法就是本模块
在固定 8,000 受控子集上的等价实现（参数同 ``wd/wr/wk/k/beta/power/knn_k/knn_power``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

__all__ = ["QueryMemory", "load_query_memory", "rerank_with_query_memory", "fuse_ce_scores"]

# 与 evals/stard_recall_lab.py 的 blend 默认参数一致；生产侧经 settings 注入。
DEFAULT_WEIGHTS = {
    "wd": 0.3,
    "wr": 1.0,
    "wk": 0.1,
    "roc_k": 10,
    "roc_beta": 1.0,
    "roc_power": 2.0,
    "knn_k": 20,
    "knn_power": 4.0,
}


@dataclass(frozen=True)
class QueryMemory:
    """不可变记忆快照；所有向量在 load 时统一 L2 归一化。"""

    memory_q: np.ndarray  # (n_memory, dim)
    memory_doc_ids: tuple[tuple[str, ...], ...]
    doc_ids: tuple[str, ...]
    doc_vectors: np.ndarray  # (n_doc, dim)

    def __post_init__(self) -> None:
        if self.memory_q.ndim != 2 or self.doc_vectors.ndim != 2:
            raise ValueError("query memory vectors must be 2-D")
        if self.memory_q.shape[1] != self.doc_vectors.shape[1]:
            raise ValueError("query/doc vector dimension mismatch")
        if len(self.memory_doc_ids) != len(self.memory_q):
            raise ValueError("memory_doc_ids length mismatch")
        if len(self.doc_ids) != len(self.doc_vectors):
            raise ValueError("doc_ids length mismatch")

    @property
    def doc_index(self) -> dict[str, int]:
        return {str(d): i for i, d in enumerate(self.doc_ids)}

    def neighbours(
        self, query_vector: np.ndarray, k: int, power: float
    ) -> list[tuple[float, int]]:
        """返回 (归一化权重, 记忆问法下标) 的 top-k 近邻；相似度负值截断为 0。"""
        q = np.asarray(query_vector, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(q))
        if norm <= 0:
            return []
        q = q / norm
        mq = self.memory_q.astype(np.float32, copy=False)
        mq = mq / (np.linalg.norm(mq, axis=1, keepdims=True) + 1e-9)
        sims = mq @ q
        width = min(int(k), len(sims))
        if width <= 0:
            return []
        top = np.argpartition(-sims, width - 1)[:width]
        top = top[np.argsort(-sims[top], kind="stable")]
        weights = np.clip(sims[top], 0.0, None) ** float(power)
        total = float(weights.sum())
        if total > 0:
            weights = weights / total
        return [(float(w), int(i)) for w, i in zip(weights, top)]

    def vote_scores(
        self, query_vector: np.ndarray, k: int, power: float
    ) -> dict[str, float]:
        """近邻历史问法的相关文档加权计票。"""
        votes: dict[str, float] = {}
        for weight, idx in self.neighbours(query_vector, k, power):
            docs = self.memory_doc_ids[idx]
            if not docs:
                continue
            share = weight / len(docs)
            for doc_id in docs:
                votes[doc_id] = votes.get(doc_id, 0.0) + share
        return votes

    def rocchio_vector(
        self, query_vector: np.ndarray, k: int, beta: float, power: float
    ) -> np.ndarray:
        """近邻相关文档向量质心回注后的 query 向量（已归一化）。"""
        q = np.asarray(query_vector, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(q))
        if norm <= 0:
            return q
        q = q / norm
        centroid = np.zeros(self.doc_vectors.shape[1], dtype=np.float32)
        index = self.doc_index
        for weight, idx in self.neighbours(query_vector, k, power):
            cols = [index[d] for d in self.memory_doc_ids[idx] if d in index]
            if cols:
                centroid += weight * self.doc_vectors[cols].mean(axis=0)
        shifted = q + float(beta) * centroid
        return shifted / (np.linalg.norm(shifted) + 1e-9)


def load_query_memory(path: str | Path) -> QueryMemory:
    """从 npz 载入记忆快照；字段缺失立即报错（不静默降级成空记忆）。"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"query memory file not found: {p}")
    with np.load(p, allow_pickle=False) as z:
        required = ("memory_q", "memory_doc_ptr", "memory_doc_flat", "doc_ids", "doc_vectors")
        missing = [k for k in required if k not in z.files]
        if missing:
            raise ValueError(f"query memory missing arrays: {missing}")
        memory_q = np.asarray(z["memory_q"], dtype=np.float32)
        doc_vectors = np.asarray(z["doc_vectors"], dtype=np.float32)
        ptr = np.asarray(z["memory_doc_ptr"], dtype=np.int64)
        flat = np.asarray(z["memory_doc_flat"], dtype=np.int64)
        doc_ids = tuple(str(x) for x in z["doc_ids"])
    if ptr.ndim != 1 or ptr.size != len(memory_q) + 1 or int(ptr[-1]) != flat.size:
        raise ValueError("query memory doc pointer table is inconsistent")
    if flat.size and (int(flat.min()) < 0 or int(flat.max()) >= len(doc_ids)):
        raise ValueError("query memory doc index out of range")
    memory_doc_ids = tuple(
        tuple(doc_ids[int(i)] for i in flat[int(ptr[j]) : int(ptr[j + 1])])
        for j in range(len(memory_q))
    )
    memory_q = memory_q / (np.linalg.norm(memory_q, axis=1, keepdims=True) + 1e-9)
    doc_vectors = doc_vectors / (np.linalg.norm(doc_vectors, axis=1, keepdims=True) + 1e-9)
    return QueryMemory(memory_q, memory_doc_ids, doc_ids, doc_vectors)


def rerank_with_query_memory(
    *,
    query_vector: np.ndarray,
    candidate_ids: Sequence[str],
    dense_scores: Sequence[float],
    candidate_vectors: np.ndarray,
    memory: QueryMemory,
    weights: dict | None = None,
    top_k: int = 5,
) -> list[int]:
    """返回候选重排后的下标顺序（长度不变，只换顺序）。

    ``dense_scores`` 方向必须与 ``candidate_vectors`` 的点积一致（越大越相关）。
    候选文档向量缺失时该候选只保留 dense 分数（fail-soft，不抛错）。
    """
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update({k: v for k, v in weights.items() if k in w})
    n = len(candidate_ids)
    if n == 0:
        return []
    dense = np.asarray(dense_scores, dtype=np.float32).reshape(-1)
    if dense.size != n:
        raise ValueError("dense_scores length mismatch")
    vecs = np.asarray(candidate_vectors, dtype=np.float32)
    if vecs.shape[0] != n:
        raise ValueError("candidate_vectors length mismatch")

    roc_q = memory.rocchio_vector(
        query_vector, int(w["roc_k"]), float(w["roc_beta"]), float(w["roc_power"])
    )
    roc_sims = vecs @ roc_q
    votes = memory.vote_scores(query_vector, int(w["knn_k"]), float(w["knn_power"]))
    vote_vec = np.asarray([votes.get(str(d), 0.0) for d in candidate_ids], dtype=np.float32)

    combined = (
        float(w["wd"]) * dense
        + float(w["wr"]) * roc_sims
        + float(w["wk"]) * vote_vec
    )
    order = np.argsort(-combined, kind="stable")
    result = [int(i) for i in order]
    if 0 < int(top_k) < len(result):
        # top_k 只用于调用方截断提示，不裁剪返回（保持与候选数一一对应）。
        pass
    return result


def fuse_ce_scores(base_scores: Sequence[float], ce_scores: Sequence[float], mu: float) -> np.ndarray:
    """候选池内 z(base) + μ·z(CE)；与实验台 ``ceblend`` 同口径。

    两者在同一组候选上做 z-score，避免量纲差异；标准差为 0 时该通道全记 0
    （与 ``rank_fusion.zscore_fuse`` 的除零口径一致）。
    """
    b = np.asarray(base_scores, dtype=np.float32).reshape(-1)
    c = np.asarray(ce_scores, dtype=np.float32).reshape(-1)
    if b.size != c.size:
        raise ValueError("base/ce score length mismatch")
    bz = (b - b.mean()) / (b.std() + 1e-9)
    cz = (c - c.mean()) / (c.std() + 1e-9)
    return bz + float(mu) * cz
