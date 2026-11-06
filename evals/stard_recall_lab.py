"""STARD 召回实验台：固定 8,000 受控子集上的重排 / 向量修正方法对照。

口径（与 evals/stard_eval.py、README §7、evals/README §4.9 完全一致）：

- 语料 = ``subset_rows(全库 55,348 条, 全部金标, 8000, seed=42)``；
- 向量 = ``.tmp/stard/emb_8000_text-embedding-bge-m3.npz`` 缓存（不重新 embedding）；
- 命中 = qrels 精确 docid，至少一条相关法条进 top-k；
- dev 308 为盲测：所有超参在 train 的留出子集（seed=42，935 训练 / 300 验证）上定，
  dev 只用 ``--split dev`` 一次性读数；
- 指标函数直接复用 ``evals.stard_eval.evaluate``，本文件不另写一份打分口径。

方法（``--method`` 可重复，参数写在冒号后 ``k=v,k=v``）：

- ``dense`` / ``bm25`` / ``rrf``：既有基线（用同一缓存重算，验证可复算）；
- ``knn``：用 train qrels 做 query→query 近邻投票，把近邻的金标法条加权补到 dense 分数上；
- ``rocchio``：近邻金标法条的 doc 向量质心加权回注到 query 向量（向量级 Rocchio）；
- ``proj``：train qrels 学 ridge 投影 q→金标 doc 质心，再插值回原 query 向量；
- ``ce``：Cross-Encoder（Infinity :7997/rerank）精排候选池；
  ``source`` 可为 ``dense`` / ``rrf`` / ``knn_union``（dense top-100 + knn 提名的 extra 条）。
- ``hgbce``：仅用 train qrels 训练 HGB 候选重排器，并与 Cross-Encoder 分数融合；
  需要外部 ``/rerank`` 服务，不作为生产默认。

示例：

  # 基线复算
  .venv/Scripts/python.exe evals/stard_recall_lab.py --method dense --method bm25 --method rrf \
      --split dev --write-summary .tmp/stard/lab_baseline.json

  # train 留出子集调参
  .venv/Scripts/python.exe evals/stard_recall_lab.py --split val \
      --method "knn:k=10,lam=0.20,power=2" --method "knn:k=10,lam=0.40,power=2"

  # dev 盲测（含 CE 精排，结果落缓存避免重复调用）
  .venv/Scripts/python.exe evals/stard_recall_lab.py --split dev --method dense \
      --method "ce:cand=100,workers=4,chars=512" \
      --ce-cache .tmp/stard/ce_dev_100.json \
      --write-summary docs/evals/stard-recall-lab-summary.json
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.perf_stats import floor_rank_quantile  # noqa: E402  (需先补 path)

from agentix.domain.policies.rank_fusion import rrf_fuse  # noqa: E402
from agentix.retrieval.infra.rag.sparse import BM25Index  # noqa: E402
from evals.stard_eval import DATA, POOL, evaluate, load, subset_rows  # noqa: E402


EMB_CACHE = DATA / "emb_8000_text-embedding-bge-m3.npz"
CE_URL_DEFAULT = "http://127.0.0.1:7997/rerank"
CE_MODEL_DEFAULT = "text-embedding-bge-reranker-v2-m3"
VAL_SEED = 42
VAL_SIZE = 300

DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "dense": {},
    "bm25": {},
    "rrf": {"k": 60},
    "knn": {"k": 10, "lam": 0.30, "power": 2.0, "mode": "vote"},
    "rocchio": {"k": 10, "beta": 0.30, "power": 2.0},
    "rocchio_knn": {"k": 10, "beta": 1.0, "power": 2.0, "lam": 0.10, "knn_k": 20},
    "blend": {
        "wd": 0.0,
        "wr": 1.0,
        "wk": 0.0,
        "k": 10,
        "beta": 1.0,
        "power": 2.0,
        "knn_k": 20,
        "knn_power": 4.0,
    },
    "rrf3": {"k": 60, "k_nn": 10, "beta": 1.0, "power": 2.0, "lam": 0.30, "knn_k": 20},
    "docexp": {"gamma": 0.5, "reg": 10.0, "mode": "mean", "wd": 0.0, "wr": 0.0, "wk": 0.0,
               "k": 10, "beta": 1.0, "power": 2.0, "knn_k": 20, "knn_power": 4.0},
    "ltr": {"model": "logreg", "C": 1.0, "n": 100, "wd": 0.3, "wr": 1.0, "wk": 0.1,
            "k": 10, "beta": 1.0, "power": 2.0, "knn_k": 20, "knn_power": 4.0},
    "hgbce": {
        "cand": 100,
        "workers": 4,
        "chars": 512,
        "source": "blend_bm25_union",
        "extra": 50,
        "blend_cand": 50,
        "feature_n": 50,
        "ce_weight": 1.0,
        "max_leaf_nodes": 31,
        "knn_k": 20,
        "knn_power": 4.0,
        "roc_k": 10,
        "roc_beta": 1.0,
        "roc_power": 2.0,
        "wd": 0.3,
        "wr": 1.0,
        "wk": 0.1,
        "knn_cand_k": 20,
        "knn_cand_power": 4.0,
    },
    "linrank": {"reg": 10.0, "wd": 0.0, "wr": 0.0, "wk": 0.0, "k": 10, "beta": 1.0,
                "power": 2.0, "knn_k": 20, "knn_power": 4.0, "idf": 0},
    "zrank": {"n": 20, "zd": 0.0, "zr": 0.0, "zk": 0.0, "zb": 0.0, "wd": 0.3,
              "wr": 1.0, "wk": 0.1, "k": 10, "beta": 1.0, "power": 2.0,
              "knn_k": 20, "knn_power": 4.0},
    "qexp": {"k": 10, "power": 2.0, "m": 5, "docm": 1, "chars": 600,
             "wexp": 1.0, "wd": 0.0, "wk": 0.0, "knn_k": 20, "knn_power": 4.0},
    "ceblend": {
        "cand": 20,
        "mu": 0.25,
        "workers": 1,
        "chars": 512,
        "source": "blend",
        "extra": 0,
        "knn_k": 10,
        "knn_power": 2.0,
        "roc_k": 10,
        "roc_beta": 1.0,
        "roc_power": 2.0,
        "wd": 0.3,
        "wr": 1.0,
        "wk": 0.1,
        "knn_cand_k": 20,
        "knn_cand_power": 4.0,
    },
    "proj": {"reg": 10.0, "alpha": 0.30, "center": 1},
    "cez": {
        "cand": 20,
        "mu": 0.5,
        "workers": 1,
        "chars": 512,
        "source": "dense",
        "extra": 0,
        "knn_k": 10,
        "knn_power": 2.0,
        "roc_k": 10,
        "roc_beta": 1.0,
        "roc_power": 2.0,
        "wd": 0.3,
        "wr": 1.0,
        "wk": 0.1,
        "knn_cand_k": 20,
        "knn_cand_power": 4.0,
    },
    "ce": {
        "cand": 100,
        "workers": 4,
        "chars": 512,
        "source": "dense",
        "extra": 0,
        "knn_k": 10,
        "knn_power": 2.0,
        "roc_k": 10,
        "roc_beta": 1.0,
        "roc_power": 2.0,
        "wd": 0.3,
        "wr": 1.0,
        "wk": 0.1,
        "knn_cand_k": 20,
        "knn_cand_power": 4.0,
    },
}


def parse_methods(specs: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """解析 ``--method "name:k=v,k=v"``；未知方法 / 未知参数立即报错。"""
    out: list[tuple[str, dict[str, Any]]] = []
    for spec in specs:
        parts = spec.split(":", 1)
        name = parts[0].strip()
        if name not in DEFAULT_PARAMS:
            raise SystemExit(f"unknown method: {name} (available: {sorted(DEFAULT_PARAMS)})")
        params = dict(DEFAULT_PARAMS[name])
        if len(parts) == 2 and parts[1].strip():
            for item in parts[1].split(","):
                key, _, value = item.strip().partition("=")
                if key not in params:
                    raise SystemExit(f"unknown param '{key}' for method '{name}'")
                default = params[key]
                if isinstance(default, int) and not isinstance(default, bool):
                    params[key] = int(value)
                elif isinstance(default, float):
                    params[key] = float(value)
                else:
                    params[key] = value
        out.append((name, params))
    return out


class StardLab:
    """共享语料 / 向量 / 候选池的实验上下文（一次加载，多方法复用）。"""

    def __init__(self, subset: int = 8000) -> None:
        rows, qs, train, dev = load()
        self.rows = subset_rows(rows, {**train, **dev}, subset)
        self.qs = qs
        self.train = train
        self.dev = dev
        self.gold = {**train, **dev}
        self.qids = sorted(self.gold)
        self.qindex = {q: i for i, q in enumerate(self.qids)}
        self.ids = [r[0] for r in self.rows]
        self.id2i = {d: i for i, d in enumerate(self.ids)}
        self.id2text = {r[0]: r[2] for r in self.rows}

        z = np.load(EMB_CACHE)
        self.D = np.asarray(z["doc"], dtype=np.float32)
        self.Q = np.asarray(z["q"], dtype=np.float32)
        if self.D.shape != (len(self.rows), 1024) or self.Q.shape != (len(self.qids), 1024):
            raise SystemExit("embedding 缓存形状与当前语料不一致，删除 .tmp/stard/emb_8000_*.npz 重跑")
        if list(z["qids"]) != list(self.qids):
            raise SystemExit("embedding 缓存的 qid 顺序与本次不一致")
        self.S = self.Q @ self.D.T  # (1543, 8000) cosine 相似度矩阵

        self.train_qids = sorted(train)
        shuffled = list(self.train_qids)
        import random

        random.Random(VAL_SEED).shuffle(shuffled)
        self.val_qids = sorted(shuffled[:VAL_SIZE])
        self.fit_qids = sorted(shuffled[VAL_SIZE:])

        t0 = time.perf_counter()
        self.dense_rank_all = self._top_from_sims(self.S, POOL)
        print(f"[lab] dense top-{POOL}: {time.perf_counter()-t0:.1f}s", flush=True)
        # BM25 全量分词约 19 s，只在方法确实需要时构建并落 .tmp 缓存（已 gitignore）。
        self._bm25_cache_path = DATA / f"bm25_{len(self.rows)}_top{POOL}.json"
        self._bm25_score_path = DATA / f"bm25_{len(self.rows)}_scores_top{POOL}.json"
        self.bm25_rank_all: dict[int, list[str]] = {}
        self.bm25_scores_all: dict[int, dict[str, float]] = {}
        self.rrf_rank_all: dict[int, list[str]] = {}
        self._last_ce_seconds: list[float] = []

    def _ensure_bm25(self) -> None:
        if self.bm25_rank_all and self.bm25_scores_all:
            return
        if self._bm25_cache_path.exists() and self._bm25_score_path.exists():
            raw = json.loads(self._bm25_cache_path.read_text(encoding="utf-8"))
            self.bm25_rank_all = {int(q): docs for q, docs in raw.items()}
            raw_s = json.loads(self._bm25_score_path.read_text(encoding="utf-8"))
            self.bm25_scores_all = {int(q): m for q, m in raw_s.items()}
        else:
            t0 = time.perf_counter()
            idx = BM25Index([(r[0], r[2], {}) for r in self.rows])
            self.bm25_rank_all = {}
            self.bm25_scores_all = {}
            for q in self.qids:
                hits = idx.search(self.qs[q], POOL)
                self.bm25_rank_all[q] = [d for d, _ in hits]
                self.bm25_scores_all[q] = {d: float(s) for d, s in hits}
            self._bm25_cache_path.write_text(
                json.dumps({str(q): d for q, d in self.bm25_rank_all.items()}),
                encoding="utf-8",
            )
            self._bm25_score_path.write_text(
                json.dumps({str(q): m for q, m in self.bm25_scores_all.items()}),
                encoding="utf-8",
            )
            print(f"[lab] BM25 top-{POOL}: {time.perf_counter()-t0:.1f}s", flush=True)
        self.rrf_rank_all = {
            q: [d for d, _ in rrf_fuse([self.bm25_rank_all[q], self.dense_rank_all[q]], k=60, top_n=POOL)]
            for q in self.qids
        }

    def _top_from_sims(
        self, sims: np.ndarray, top: int, qids: list[int] | None = None
    ) -> dict[int, list[str]]:
        out: dict[int, list[str]] = {}
        target = self.qids if qids is None else qids
        n_doc = sims.shape[1]
        width = min(top, n_doc - 1)
        for j, qid in enumerate(target):
            cand = np.argpartition(-sims[j], width)[: width + 1]
            cand = cand[np.argsort(-sims[j][cand], kind="stable")][:top]
            out[qid] = [self.ids[i] for i in cand]
        return out

    def split_qids(self, split: str, limit: int) -> tuple[list[int], list[int]]:
        """返回 (评测 qid 列表, 可用来拟合/近邻的 train qid 列表)。"""
        if split == "dev":
            qids, fit = sorted(self.dev), list(self.train_qids)
        elif split == "dev60":
            qids, fit = sorted(self.dev)[:60], list(self.train_qids)
        elif split == "val":
            qids, fit = list(self.val_qids), list(self.fit_qids)
        elif split == "train":
            qids, fit = list(self.train_qids), list(self.train_qids)
        elif split == "all":
            qids, fit = list(self.qids), list(self.train_qids)
        else:
            raise SystemExit(f"unknown split: {split}")
        if limit > 0:
            qids = qids[:limit]
        return qids, fit

    # ------------------------------------------------------------------
    # train qrels 驱动的方法
    # ------------------------------------------------------------------

    def _neighbors(self, qids_eval: list[int], fit: list[int], k: int, power: float) -> list[list[tuple[float, int]]]:
        """每个评测 query 在 fit query 集里的 top-k 近邻（排除自身），返回 (权重, qid)。"""
        fit_idx = np.asarray([self.qindex[q] for q in fit], dtype=np.int64)
        Qf = self.Q[fit_idx]
        Qf /= np.linalg.norm(Qf, axis=1, keepdims=True) + 1e-9
        fit_set = set(fit)
        out: list[list[tuple[float, int]]] = []
        for qid in qids_eval:
            qv = self.Q[self.qindex[qid]]
            sims = Qf @ qv
            if qid in fit_set:
                sims[fit.index(qid)] = -2.0
            width = min(k, len(fit))
            top = np.argpartition(-sims, width - 1)[:width]
            top = top[np.argsort(-sims[top], kind="stable")]
            weights = np.clip(sims[top], 0.0, None) ** power
            total = float(weights.sum())
            if total > 0:
                weights = weights / total
            out.append([(float(w), int(fit[int(i)])) for w, i in zip(weights, top)])
        return out

    def knn_votes(self, qids_eval: list[int], fit: list[int], k: int, power: float) -> np.ndarray:
        """query→query 近邻的金标投票矩阵，形状 (len(qids_eval), 8000)。"""
        votes = np.zeros((len(qids_eval), len(self.ids)), dtype=np.float32)
        for row, neighbors in enumerate(self._neighbors(qids_eval, fit, k, power)):
            for weight, nqid in neighbors:
                docs = self.train.get(nqid, set())
                if not docs:
                    continue
                share = weight / len(docs)
                for doc in docs:
                    col = self.id2i.get(doc)
                    if col is not None:
                        votes[row, col] += share
        return votes

    def knn_rank(self, qids_eval: list[int], fit: list[int], p: dict[str, Any]) -> dict[int, list[str]]:
        votes = self.knn_votes(qids_eval, fit, int(p["k"]), float(p["power"]))
        lam = float(p["lam"])
        if p["mode"] == "vote_only":
            sims = votes
        else:
            rows = [self.qindex[q] for q in qids_eval]
            sims = self.S[rows] + lam * votes
        return self._top_from_sims(sims, POOL, qids_eval)

    def _rocchio_sims(
        self,
        qids_eval: list[int],
        fit: list[int],
        k: int,
        beta: float,
        power: float,
        D: np.ndarray | None = None,
    ) -> np.ndarray:
        """近邻金标 doc 向量质心回注 query 向量后的全库相似度矩阵。"""
        doc_mat = self.D if D is None else D
        neighbors = self._neighbors(qids_eval, fit, k, power)
        shifted = np.zeros((len(qids_eval), doc_mat.shape[1]), dtype=np.float32)
        for row, nb in enumerate(neighbors):
            centroid = np.zeros(doc_mat.shape[1], dtype=np.float32)
            for weight, nqid in nb:
                docs = [self.id2i[d] for d in self.train.get(nqid, set()) if d in self.id2i]
                if docs:
                    centroid += weight * doc_mat[docs].mean(axis=0)
            shifted[row] = centroid
        q = self.Q[[self.qindex[x] for x in qids_eval]]
        qnew = q + beta * shifted
        qnew /= np.linalg.norm(qnew, axis=1, keepdims=True) + 1e-9
        return qnew @ doc_mat.T

    def _doc_matrix(self, fit: list[int], gamma: float, reg: float, mode: str) -> np.ndarray:
        """用 train qrels 把 doc 向量向 query 侧平移，得到增强后的 doc 矩阵。

        ``mean``：D' = normalize(D + γ · 引用过该法条的 train query 向量均值)；
        ``ridge``：先在 (gold doc 向量 → train query 向量) 对上学一个岭回归 W，
        再令 D' = normalize(D + γ · D @ W)；``both`` 为两者相加。
        """
        if mode not in ("mean", "ridge", "both"):
            raise SystemExit(f"unknown docexp mode: {mode}")
        base = self.D.copy()
        if mode in ("mean", "both"):
            acc = np.zeros_like(self.D)
            cnt = np.zeros((len(self.ids), 1), dtype=np.float32)
            for qid in fit:
                qv = self.Q[self.qindex[qid]]
                for doc in self.train.get(qid, set()):
                    col = self.id2i.get(doc)
                    if col is not None:
                        acc[col] += qv
                        cnt[col, 0] += 1
            mean_q = acc / np.maximum(cnt, 1.0)
            base = base + gamma * mean_q
        if mode in ("ridge", "both"):
            src: list[np.ndarray] = []
            tgt: list[np.ndarray] = []
            for qid in fit:
                qv = self.Q[self.qindex[qid]]
                for doc in self.train.get(qid, set()):
                    col = self.id2i.get(doc)
                    if col is not None:
                        src.append(self.D[col])
                        tgt.append(qv)
            X = np.asarray(src, dtype=np.float32)
            Y = np.asarray(tgt, dtype=np.float32)
            gram = X.T @ X + reg * np.eye(X.shape[1], dtype=np.float32)
            W = np.linalg.solve(gram, X.T @ Y)
            base = base + gamma * (self.D @ W)
        base /= np.linalg.norm(base, axis=1, keepdims=True) + 1e-9
        return base.astype(np.float32)

    def docexp_rank(self, qids_eval: list[int], fit: list[int], p: dict[str, Any]) -> dict[int, list[str]]:
        """doc 侧表示增强，并可与 Rocchio / kNN 分数线性组合。"""
        D2 = self._doc_matrix(fit, float(p["gamma"]), float(p["reg"]), str(p["mode"]))
        rows = [self.qindex[q] for q in qids_eval]
        sims = self.Q[rows] @ D2.T
        weighted = float(p["wd"]) * sims
        if p["wr"]:
            weighted = weighted + float(p["wr"]) * self._rocchio_sims(
                qids_eval, fit, int(p["k"]), float(p["beta"]), float(p["power"]), D2
            )
        if p["wk"]:
            weighted = weighted + float(p["wk"]) * self.knn_votes(
                qids_eval, fit, int(p["knn_k"]), float(p["knn_power"])
            )
        return self._top_from_sims(weighted if (p["wr"] or p["wk"]) else sims, POOL, qids_eval)

    def rocchio_rank(self, qids_eval: list[int], fit: list[int], p: dict[str, Any]) -> dict[int, list[str]]:
        """向量级 Rocchio：近邻金标 doc 质心回注 query 后重排。"""
        sims = self._rocchio_sims(
            qids_eval, fit, int(p["k"]), float(p["beta"]), float(p["power"])
        )
        return self._top_from_sims(sims, POOL, qids_eval)

    def rocchio_knn_rank(
        self, qids_eval: list[int], fit: list[int], p: dict[str, Any]
    ) -> dict[int, list[str]]:
        """Rocchio 修正后的相似度再叠加近邻金标投票。"""
        sims = self._rocchio_sims(
            qids_eval, fit, int(p["k"]), float(p["beta"]), float(p["power"])
        )
        votes = self.knn_votes(qids_eval, fit, int(p["knn_k"]), float(p["power"]))
        return self._top_from_sims(sims + float(p["lam"]) * votes, POOL, qids_eval)

    def _semantic_channels(
        self, qids_eval: list[int], fit: list[int], p: dict[str, Any]
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, dict[int, list[str]]]]:
        """LTR 用的三路分数 / 三路排名（dense、Rocchio、kNN 投票）。"""
        rows = [self.qindex[q] for q in qids_eval]
        dense_sims = self.S[rows]
        roc_sims = self._rocchio_sims(
            qids_eval, fit, int(p["k"]), float(p["beta"]), float(p["power"])
        )
        votes = self.knn_votes(qids_eval, fit, int(p["knn_k"]), float(p["knn_power"]))
        knn_sims = float(p["wd"]) * dense_sims + float(p["wr"]) * roc_sims + float(p["wk"]) * votes
        n = int(p["n"])
        roc_rank = self._top_from_sims(roc_sims, n, qids_eval)
        knn_rank = self._top_from_sims(votes, n, qids_eval)
        dense_rank = self._top_from_sims(dense_sims, n, qids_eval)
        blend_rank = self._top_from_sims(knn_sims, n, qids_eval)
        return dense_sims, roc_sims, votes, {
            "dense": dense_rank, "roc": roc_rank, "knn": knn_rank, "blend": blend_rank
        }

    def _ltr_features(
        self,
        qids: list[int],
        fit: list[int],
        p: dict[str, Any],
        with_labels: bool,
    ) -> tuple[list[np.ndarray], list[list[str]], np.ndarray | None]:
        """构造候选集与逐 (q,d) 特征；标签只给训练用，dev 评测不读 qrels。"""
        dense_sims, roc_sims, votes, ranks = self._semantic_channels(qids, fit, p)
        n = int(p["n"])
        feats: list[np.ndarray] = []
        cands: list[list[str]] = []
        labels: list[np.ndarray] = []
        for row, qid in enumerate(qids):
            merged: list[str] = []
            seen: set[str] = set()
            for channel in ("dense", "roc", "knn", "blend"):
                for d in ranks[channel][qid][:n]:
                    if d not in seen:
                        seen.add(d)
                        merged.append(d)
            cols = np.asarray([self.id2i[d] for d in merged], dtype=np.int64)
            r_dense = {d: i + 1 for i, d in enumerate(ranks["dense"][qid])}
            r_roc = {d: i + 1 for i, d in enumerate(ranks["roc"][qid])}
            r_knn = {d: i + 1 for i, d in enumerate(ranks["knn"][qid])}
            f1 = dense_sims[row, cols]
            f2 = roc_sims[row, cols]
            f3 = votes[row, cols]
            f4 = np.asarray([1.0 / (60.0 + r_dense.get(d, n + 1)) for d in merged], dtype=np.float32)
            f5 = np.asarray([1.0 / (60.0 + r_roc.get(d, n + 1)) for d in merged], dtype=np.float32)
            f6 = np.asarray([1.0 / (60.0 + r_knn.get(d, n + 1)) for d in merged], dtype=np.float32)
            f7 = np.asarray([1.0 - min(r_dense.get(d, n + 1), r_roc.get(d, n + 1), r_knn.get(d, n + 1)) / (n + 1.0) for d in merged], dtype=np.float32)
            f8 = np.asarray([sum(r.get(d, n + 1) <= 10 for r in (r_dense, r_roc, r_knn)) for d in merged], dtype=np.float32)
            f9 = f1 * f3
            f10 = f2 * f3
            f11 = float(p["wd"]) * f1 + float(p["wr"]) * f2 + float(p["wk"]) * f3
            X = np.column_stack((f1, f2, f3, f4, f5, f6, f7, f8, f9, f10, f11)).astype(np.float32)
            feats.append(X)
            cands.append(merged)
            if with_labels:
                g = self.train.get(qid, set())
                labels.append(np.asarray([1.0 if d in g else 0.0 for d in merged], dtype=np.float32))
        y = np.concatenate(labels) if with_labels else None
        return feats, cands, y

    def _hgbce_features(
        self,
        qids: list[int],
        fit: list[int],
        p: dict[str, Any],
        candidates: list[list[str]],
        with_labels: bool,
    ) -> tuple[list[np.ndarray], np.ndarray | None]:
        """Build per-query normalized retrieval features for the train-only HGB reranker."""
        rows = [self.qindex[q] for q in qids]
        dense = self.S[rows]
        roc = self._rocchio_sims(
            qids, fit, int(p["roc_k"]), float(p["roc_beta"]), float(p["roc_power"])
        )
        votes = self.knn_votes(qids, fit, int(p["knn_cand_k"]), float(p["knn_cand_power"]))
        blend = float(p["wd"]) * dense + float(p["wr"]) * roc + float(p["wk"]) * votes
        feature_n = int(p["feature_n"])
        ranks = {
            "dense": self._top_from_sims(dense, feature_n, qids),
            "roc": self._top_from_sims(roc, feature_n, qids),
            "knn": self._top_from_sims(votes, feature_n, qids),
            "blend": self._top_from_sims(blend, feature_n, qids),
        }
        features: list[np.ndarray] = []
        labels: list[np.ndarray] = []
        for row, (qid, docs) in enumerate(zip(qids, candidates)):
            rank_maps = {
                name: {doc: rank + 1 for rank, doc in enumerate(channel[qid])}
                for name, channel in ranks.items()
            }
            rank_maps["bm25"] = {
                doc: rank + 1
                for rank, doc in enumerate(self.bm25_rank_all[qid])
            }
            cols = np.asarray([self.id2i[doc] for doc in docs], dtype=np.int64)

            def zscore(values: np.ndarray) -> np.ndarray:
                return (values - values.mean()) / (values.std() + 1e-9)

            signals = [
                zscore(dense[row, cols].astype(np.float32)),
                zscore(roc[row, cols].astype(np.float32)),
                zscore(votes[row, cols].astype(np.float32)),
                zscore(blend[row, cols].astype(np.float32)),
                zscore(
                    np.asarray(
                        [self.bm25_scores_all[qid].get(doc, 0.0) for doc in docs],
                        dtype=np.float32,
                    )
                ),
            ]
            signals.extend(
                np.asarray(
                    [1.0 / (60.0 + rank_maps[name].get(doc, 101)) for doc in docs],
                    dtype=np.float32,
                )
                for name in ("dense", "roc", "knn", "blend", "bm25")
            )
            signals.extend(
                [[float(doc in rank_maps[name]) for doc in docs] for name in ("dense", "roc", "knn", "bm25")]
            )
            features.append(np.column_stack(signals).astype(np.float32))
            if with_labels:
                gold = self.train.get(qid, set())
                labels.append(np.asarray([doc in gold for doc in docs], dtype=np.int8))
        return features, np.concatenate(labels) if with_labels else None

    def hgbce_rank(
        self,
        qids_eval: list[int],
        fit: list[int],
        p: dict[str, Any],
        url: str,
        model_name: str,
        cache_path: Path | None,
    ) -> dict[int, list[str]]:
        """Train a query-level HGB reranker on train qrels and fuse its score with CE."""
        fit_set = set(fit)
        if set(qids_eval).intersection(fit_set):
            raise ValueError("hgbce requires evaluation queries to be disjoint from training queries")
        if not fit_set or not fit_set.issubset(self.train):
            raise ValueError("hgbce training queries must come from the labeled train split")

        from sklearn.ensemble import HistGradientBoostingClassifier

        train_qids = list(fit)
        train_candidates = self._candidate_lists(train_qids, train_qids, p)
        train_features, train_labels = self._hgbce_features(
            train_qids, train_qids, p, train_candidates, with_labels=True
        )
        if train_labels is None:
            raise RuntimeError("HGB reranker training labels were not constructed")
        ranker = HistGradientBoostingClassifier(
            max_iter=150,
            max_leaf_nodes=int(p["max_leaf_nodes"]),
            learning_rate=0.08,
            l2_regularization=1.0,
            class_weight="balanced",
            random_state=42,
        )
        ranker.fit(np.concatenate(train_features), train_labels)

        candidates, ce_scores = self._ce_scores(
            qids_eval, fit, p, url, model_name, cache_path
        )
        eval_features, _ = self._hgbce_features(
            qids_eval, fit, p, candidates, with_labels=False
        )
        out: dict[int, list[str]] = {}
        for row, qid in enumerate(qids_eval):
            docs = candidates[row]
            head_n = min(len(docs), int(p["cand"]))
            predicted = ranker.predict_proba(eval_features[row])[:, 1]
            ce = np.asarray(ce_scores[qid], dtype=np.float32)

            def zscore(values: np.ndarray) -> np.ndarray:
                return (values - values.mean()) / (values.std() + 1e-9)

            combined = zscore(predicted[:head_n]) + float(p["ce_weight"]) * zscore(ce)
            order = np.argsort(-combined, kind="stable")
            out[qid] = [docs[i] for i in order] + docs[head_n:]
        return out

    def hgbce_all_rank(
        self,
        qids_eval: list[int],
        fit: list[int],
        p: dict[str, Any],
        url: str,
        model_name: str,
        cache_path: Path | None,
    ) -> dict[int, list[str]]:
        """Evaluate train queries out-of-fold and dev queries against all train labels."""
        from sklearn.model_selection import KFold

        train_qids = sorted(set(qids_eval).intersection(fit))
        dev_qids = [qid for qid in qids_eval if qid in self.dev]
        if set(train_qids) != set(fit) or set(qids_eval) != set(train_qids).union(dev_qids):
            raise ValueError("split=all requires exactly the official train and dev queries")

        fold_ids: dict[int, int] = {}
        rankings: dict[int, list[str]] = {}
        splitter = KFold(n_splits=3, shuffle=True, random_state=2026)
        qid_array = np.asarray(train_qids, dtype=np.int64)
        for fold, (fit_idx, eval_idx) in enumerate(splitter.split(qid_array), start=1):
            fold_fit = qid_array[fit_idx].tolist()
            fold_eval = qid_array[eval_idx].tolist()
            rankings.update(
                self.hgbce_rank(
                    fold_eval, fold_fit, p, url, model_name, cache_path
                )
            )
            fold_ids.update(dict.fromkeys(fold_eval, fold))

        if dev_qids:
            rankings.update(
                self.hgbce_rank(dev_qids, train_qids, p, url, model_name, cache_path)
            )
        self._hgbce_evaluation_protocol = {
            "train": "3-fold query-level out-of-fold evaluation",
            "dev": "single evaluation trained on all official train queries",
            "fold_seed": 2026,
            "fold_sizes": {
                str(fold): sum(value == fold for value in fold_ids.values())
                for fold in (1, 2, 3)
            },
        }
        return rankings

    def _qexp_embeddings(
        self,
        qids_eval: list[int],
        fit: list[int],
        p: dict[str, Any],
        url: str,
        model: str,
    ) -> np.ndarray:
        """把 train 近邻问法 + 其金标法条标题拼进 query 文本后重新 embedding。

        只用 train qrels 与 train query 文本；dev 只贡献自己的原句。
        结果按 (评测 qid 列表, fit 规模, 全部参数) 落 .tmp 缓存，避免重复打服务。
        """
        import hashlib

        from evals.stard_eval import embed_all

        k = int(p["k"])
        power = float(p["power"])
        m = int(p["m"])
        docm = int(p["docm"])
        chars = int(p["chars"])
        signature = json.dumps(
            [qids_eval, len(fit), k, power, m, docm, chars, model], ensure_ascii=False
        )
        digest = hashlib.sha1(signature.encode("utf-8")).hexdigest()[:12]
        cache = DATA / f"qexp_{digest}.npz"
        if cache.exists():
            return np.asarray(np.load(cache)["q"], dtype=np.float32)
        neighbors = self._neighbors(qids_eval, fit, k, power)
        texts: list[str] = []
        for qid, nb in zip(qids_eval, neighbors):
            parts = [self.qs[qid]]
            for _weight, nqid in nb[:m]:
                parts.append(self.qs[nqid])
            if docm > 0:
                for _weight, nqid in nb[:docm]:
                    for d in sorted(self.train.get(nqid, set())):
                        if d in self.id2text:
                            parts.append(self.id2text[d].split(" ", 1)[0])
            texts.append(" ".join(parts)[:chars])
        print(f"[qexp] embedding {len(texts)} expanded queries via {url}", flush=True)

        class _Args:
            def __init__(self, embedding_url: str, model: str) -> None:
                self.embedding_url = embedding_url
                self.model = model

        args = _Args(url, model)
        raw = np.asarray(embed_all(texts, args, batch=64), dtype=np.float32)
        raw /= np.linalg.norm(raw, axis=1, keepdims=True) + 1e-9
        np.savez_compressed(cache, q=raw.astype(np.float16))
        return raw.astype(np.float32)

    def qexp_rank(
        self,
        qids_eval: list[int],
        fit: list[int],
        p: dict[str, Any],
        url: str,
        model: str,
    ) -> dict[int, list[str]]:
        """扩展 query 向量重排，可与 dense / kNN 投票线性组合。"""
        Qexp = self._qexp_embeddings(qids_eval, fit, p, url, model)
        rows = [self.qindex[q] for q in qids_eval]
        sims = float(p["wexp"]) * (Qexp @ self.D.T)
        if p["wd"]:
            sims = sims + float(p["wd"]) * self.S[rows]
        if p["wk"]:
            sims = sims + float(p["wk"]) * self.knn_votes(
                qids_eval, fit, int(p["knn_k"]), float(p["knn_power"])
            )
        return self._top_from_sims(sims, POOL, qids_eval)

    def zrank_rank(self, qids_eval: list[int], fit: list[int], p: dict[str, Any]) -> dict[int, list[str]]:
        """候选池内逐题 z-score 融合：blend 排序出候选，再按多路归一化分数重排。"""
        rows = [self.qindex[q] for q in qids_eval]
        dense = self.S[rows]
        roc = self._rocchio_sims(
            qids_eval, fit, int(p["k"]), float(p["beta"]), float(p["power"])
        )
        votes = self.knn_votes(qids_eval, fit, int(p["knn_k"]), float(p["knn_power"]))
        blend = (
            float(p["wd"]) * dense
            + float(p["wr"]) * roc
            + float(p["wk"]) * votes
        )
        n = int(p["n"])
        cand_rank = self._top_from_sims(blend, POOL, qids_eval)
        need_bm25 = float(p["zb"]) != 0.0
        if need_bm25:
            self._ensure_bm25()
        channels = (
            (float(p["zd"]), dense),
            (float(p["zr"]), roc),
            (float(p["zk"]), votes),
        )
        out: dict[int, list[str]] = {}
        for j, qid in enumerate(qids_eval):
            docs = cand_rank[qid][:n]
            cols = [self.id2i[d] for d in docs]
            score = np.asarray(blend[j, cols], dtype=np.float32)
            score = (score - score.mean()) / (score.std() + 1e-9)
            for weight, mat in channels:
                if weight == 0.0:
                    continue
                sig = np.asarray(mat[j, cols], dtype=np.float32)
                z = (sig - sig.mean()) / (sig.std() + 1e-9)
                score = score + weight * z
            if need_bm25:
                bm = self.bm25_scores_all[qid]
                sig = np.asarray([bm.get(d, 0.0) for d in docs], dtype=np.float32)
                z = (sig - sig.mean()) / (sig.std() + 1e-9)
                score = score + float(p["zb"]) * z
            order = np.argsort(-score, kind="stable")
            out[qid] = [docs[i] for i in order] + cand_rank[qid][n:]
        return out

    def linrank_rank(self, qids_eval: list[int], fit: list[int], p: dict[str, Any]) -> dict[int, list[str]]:
        """岭回归学 query → 全库相关性向量（只用 train qrels），再与语义分数线性组合。"""
        fit_idx = [self.qindex[q] for q in fit]
        Qf = self.Q[fit_idx]
        Y = np.zeros((len(fit), len(self.ids)), dtype=np.float32)
        for row, qid in enumerate(fit):
            docs = [self.id2i[d] for d in self.train.get(qid, set()) if d in self.id2i]
            if docs:
                Y[row, docs] = 1.0 / len(docs)
        if p["idf"]:
            freq = (Y > 0).sum(axis=0)
            idf = np.log((len(fit) + 1.0) / (freq + 1.0))
            Y = Y * idf[None, :]
        gram = Qf.T @ Qf + float(p["reg"]) * np.eye(Qf.shape[1], dtype=np.float32)
        W = np.linalg.solve(gram, Qf.T @ Y)
        q = self.Q[[self.qindex[x] for x in qids_eval]]
        scores = q @ W
        if p["wd"]:
            scores = scores + float(p["wd"]) * self.S[[self.qindex[x] for x in qids_eval]]
        if p["wr"]:
            scores = scores + float(p["wr"]) * self._rocchio_sims(
                qids_eval, fit, int(p["k"]), float(p["beta"]), float(p["power"])
            )
        if p["wk"]:
            scores = scores + float(p["wk"]) * self.knn_votes(
                qids_eval, fit, int(p["knn_k"]), float(p["knn_power"])
            )
        return self._top_from_sims(scores, POOL, qids_eval)

    def ltr_rank(self, qids_eval: list[int], fit: list[int], p: dict[str, Any]) -> dict[int, list[str]]:
        """在 train qrels 上拟合逐 (q,d) 重排模型，dev 只做预测（盲测）。"""
        import warnings

        from sklearn.exceptions import ConvergenceWarning
        from sklearn.linear_model import LogisticRegression

        Xtr, _, ytr = self._ltr_features(fit, fit, p, with_labels=True)
        X_train = np.concatenate(Xtr, axis=0)
        assert ytr is not None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ConvergenceWarning)
            if p["model"] == "logreg":
                model = LogisticRegression(C=float(p["C"]), max_iter=400, class_weight="balanced")
                model.fit(X_train, ytr)
                predict = lambda X: model.decision_function(X)  # noqa: E731
            elif p["model"] == "hgb":
                from sklearn.ensemble import HistGradientBoostingClassifier

                model = HistGradientBoostingClassifier(max_iter=150, max_depth=3, learning_rate=0.08)
                model.fit(X_train, ytr)
                predict = lambda X: model.predict_proba(X)[:, 1]  # noqa: E731
            else:
                raise SystemExit(f"unknown ltr model: {p['model']}")

        Xev, cands, _ = self._ltr_features(qids_eval, fit, p, with_labels=False)
        out: dict[int, list[str]] = {}
        for row, qid in enumerate(qids_eval):
            scores = predict(Xev[row])
            order = np.argsort(-scores, kind="stable")[:POOL]
            out[qid] = [cands[row][i] for i in order]
        return out

    def blend_scores(
        self, qids_eval: list[int], fit: list[int], p: dict[str, Any]
    ) -> np.ndarray:
        """dense / Rocchio / kNN 三路线性组合分数矩阵，形状 (n_eval, n_docs)。

        ``blend_rank``（排序视图）、``ceblend_rank`` 与
        ``evals/stard_ce_fusion.py``（同候选集 z 融合）共用这一处实现，
        避免同一分数在实验台与融合脚本里出现两套口径。
        """
        rows = [self.qindex[q] for q in qids_eval]
        dense = self.S[rows]
        roc = self._rocchio_sims(
            qids_eval, fit, int(p["k"]), float(p["beta"]), float(p["power"])
        )
        votes = self.knn_votes(
            qids_eval, fit, int(p["knn_k"]), float(p["knn_power"])
        )
        return (
            float(p["wd"]) * dense
            + float(p["wr"]) * roc
            + float(p["wk"]) * votes
        )

    def blend_rank(self, qids_eval: list[int], fit: list[int], p: dict[str, Any]) -> dict[int, list[str]]:
        """dense / Rocchio / kNN 三路分数的线性组合。"""
        return self._top_from_sims(self.blend_scores(qids_eval, fit, p), POOL, qids_eval)

    def rrf3_rank(self, qids_eval: list[int], fit: list[int], p: dict[str, Any]) -> dict[int, list[str]]:
        """dense / Rocchio / kNN 三路 RRF 融合（只用名次，不用分数尺度）。"""
        roc = self.rocchio_rank(
            qids_eval, fit, {"k": p["k_nn"], "beta": p["beta"], "power": p["power"]}
        )
        knn = self.knn_rank(
            qids_eval,
            fit,
            {"k": p["knn_k"], "lam": p["lam"], "power": p["power"], "mode": "vote"},
        )
        return {
            q: [
                d
                for d, _ in rrf_fuse(
                    [self.dense_rank_all[q], roc[q], knn[q]],
                    k=int(p["k"]),
                    top_n=POOL,
                )
            ]
            for q in qids_eval
        }

    def proj_rank(self, qids_eval: list[int], fit: list[int], p: dict[str, Any]) -> dict[int, list[str]]:
        """ridge 回归 q→金标 doc 质心，投影向量插值回原 query。"""
        reg = float(p["reg"])
        alpha = float(p["alpha"])
        fit_idx = [self.qindex[q] for q in fit]
        Qf = self.Q[fit_idx]
        G = np.zeros((len(fit), self.D.shape[1]), dtype=np.float32)
        for row, qid in enumerate(fit):
            docs = [self.id2i[d] for d in self.train.get(qid, set()) if d in self.id2i]
            if docs:
                G[row] = self.D[docs].mean(axis=0)
        if p["center"]:
            q_mean = Qf.mean(axis=0)
            g_mean = G.mean(axis=0)
            Qc, Gc = Qf - q_mean, G - g_mean
        else:
            q_mean = np.zeros(self.D.shape[1], dtype=np.float32)
            g_mean = np.zeros(self.D.shape[1], dtype=np.float32)
            Qc, Gc = Qf, G
        gram = Qc.T @ Qc + reg * np.eye(Qc.shape[1], dtype=np.float32)
        W = np.linalg.solve(gram, Qc.T @ Gc)
        q = self.Q[[self.qindex[x] for x in qids_eval]]
        projected = (q - q_mean) @ W + g_mean
        qnew = (1.0 - alpha) * q + alpha * projected
        qnew /= np.linalg.norm(qnew, axis=1, keepdims=True) + 1e-9
        return self._top_from_sims(qnew @ self.D.T, POOL, qids_eval)

    # ------------------------------------------------------------------
    # Cross-Encoder 精排
    # ------------------------------------------------------------------

    def _candidate_lists(self, qids_eval: list[int], fit: list[int], p: dict[str, Any]) -> list[list[str]]:
        source = str(p["source"])
        if source == "dense":
            base = [list(self.dense_rank_all[q]) for q in qids_eval]
        elif source == "rrf":
            self._ensure_bm25()
            base = [list(self.rrf_rank_all[q]) for q in qids_eval]
        elif source == "bm25":
            self._ensure_bm25()
            base = [list(self.bm25_rank_all[q]) for q in qids_eval]
        elif source == "knn_union":
            votes = self.knn_votes(qids_eval, fit, int(p["knn_k"]), float(p["knn_power"]))
            extra = int(p["extra"])
            base = []
            for row, qid in enumerate(qids_eval):
                dense = list(self.dense_rank_all[qid])
                if extra > 0:
                    order = np.argsort(-votes[row], kind="stable")
                    dense_set = set(dense)
                    added = [self.ids[i] for i in order if votes[row, i] > 0 and self.ids[i] not in dense_set][:extra]
                    base.append(dense[: POOL - len(added)] + added)
                else:
                    base.append(dense)
        elif source in ("blend", "dense_blend_union"):
            blend_rank = self.blend_rank(
                qids_eval,
                fit,
                {
                    "wd": p["wd"],
                    "wr": p["wr"],
                    "wk": p["wk"],
                    "k": p["roc_k"],
                    "beta": p["roc_beta"],
                    "power": p["roc_power"],
                    "knn_k": p["knn_cand_k"],
                    "knn_power": p["knn_cand_power"],
                },
            )
            if source == "blend":
                base = [list(blend_rank[q]) for q in qids_eval]
            else:
                extra = int(p["extra"])
                base = []
                for qid in qids_eval:
                    dense = list(self.dense_rank_all[qid])
                    dense_set = set(dense)
                    added = [d for d in blend_rank[qid] if d not in dense_set][:extra]
                    base.append(dense[: POOL - len(added)] + added)
        elif source == "blend_bm25_union":
            self._ensure_bm25()
            blend_rank = self.blend_rank(
                qids_eval,
                fit,
                {
                    "wd": p["wd"],
                    "wr": p["wr"],
                    "wk": p["wk"],
                    "k": p["roc_k"],
                    "beta": p["roc_beta"],
                    "power": p["roc_power"],
                    "knn_k": p["knn_cand_k"],
                    "knn_power": p["knn_cand_power"],
                },
            )
            blend_cand = int(p["blend_cand"])
            extra = int(p["extra"])
            base = []
            for qid in qids_eval:
                docs = list(blend_rank[qid][:blend_cand])
                seen = set(docs)
                docs.extend(
                    d for d in self.bm25_rank_all[qid][:extra] if d not in seen
                )
                base.append(docs)
        elif source in ("rocchio", "dense_roc_union"):
            roc_rank = self.rocchio_rank(
                qids_eval,
                fit,
                {"k": p["roc_k"], "beta": p["roc_beta"], "power": p["roc_power"]},
            )
            if source == "rocchio":
                base = [list(roc_rank[q]) for q in qids_eval]
            else:
                extra = int(p["extra"])
                base = []
                for qid in qids_eval:
                    dense = list(self.dense_rank_all[qid])
                    dense_set = set(dense)
                    added = [d for d in roc_rank[qid] if d not in dense_set][:extra]
                    base.append(dense[: POOL - len(added)] + added)
        else:
            raise SystemExit(f"unknown ce source: {source}")
        return base

    def _ce_scores(
        self,
        qids_eval: list[int],
        fit: list[int],
        p: dict[str, Any],
        url: str,
        model: str,
        cache_path: Path | None,
    ) -> tuple[list[list[str]], dict[int, list[float]]]:
        """取候选池的 Cross-Encoder 分数（带 .tmp 缓存）；返回 (候选列表, 逐题分数)。

        缓存值形如 ``{"scores": [...], "seconds": float}``；seconds 是单题上游
        wall time，用于报告 P50/P95 延迟。每 10 题落一次盘，中断不丢已完成部分。
        """
        cand = int(p["cand"])
        workers = max(1, int(p["workers"]))
        chars = int(p["chars"])
        base = self._candidate_lists(qids_eval, fit, p)

        cache: dict[str, dict] = {}
        if cache_path is not None and cache_path.exists():
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        def key_of(qid: int) -> str:
            return (
                f"{qid}|{cand}|{chars}|{p['source']}|{p['extra']}|{p['knn_k']}|"
                f"{p['knn_power']}|{p['roc_k']}|{p['roc_beta']}|{p['roc_power']}|"
                f"{p['wd']}|{p['wr']}|{p['wk']}|{p['knn_cand_k']}|{p['knn_cand_power']}|{model}|{len(fit)}"
            )
        todo = [(qid, base[i][:cand]) for i, qid in enumerate(qids_eval) if key_of(qid) not in cache]
        if todo:
            print(f"[ce] {len(todo)} queries x <= {cand} docs, workers={workers}", flush=True)
            t0 = time.perf_counter()

            def _one(item: tuple[int, list[str]]) -> tuple[int, str, list[float], float]:
                qid, docs = item
                payload = json.dumps(
                    {
                        "model": model,
                        "query": self.qs[qid],
                        "documents": [self.id2text[d][:chars] for d in docs],
                        "top_n": len(docs),
                    }
                ).encode("utf-8")
                req = urllib.request.Request(
                    url, data=payload, headers={"Content-Type": "application/json"}
                )
                started = time.perf_counter()
                for attempt in range(3):
                    try:
                        with urllib.request.urlopen(req, timeout=180) as r:
                            body = json.load(r)
                        break
                    except Exception:
                        if attempt == 2:
                            raise
                        time.sleep(1.0)
                elapsed = time.perf_counter() - started
                rows = body["results"]
                scores = [0.0] * len(docs)
                for row in rows:
                    i = int(row["index"])
                    if 0 <= i < len(docs):
                        scores[i] = float(row.get("relevance_score", 0.0))
                return qid, key_of(qid), scores, elapsed

            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                for done, (qid, key, scores, elapsed) in enumerate(pool.map(_one, todo), 1):
                    cache[key] = {"scores": scores, "seconds": round(elapsed, 4)}
                    if done % 10 == 0 and cache_path is not None:
                        cache_path.parent.mkdir(parents=True, exist_ok=True)
                        cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
                    if done % 25 == 0 or done == len(todo):
                        print(f"  ce {done}/{len(todo)} ({time.perf_counter()-t0:.0f}s)", flush=True)
            if cache_path is not None:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        scores_by_qid = {qid: cache[key_of(qid)]["scores"] for qid in qids_eval}
        self._last_ce_seconds = [
            float(cache[key_of(qid)].get("seconds", 0.0)) for qid in qids_eval
        ]
        return base, scores_by_qid

    def ce_rank(
        self,
        qids_eval: list[int],
        fit: list[int],
        p: dict[str, Any],
        url: str,
        model: str,
        cache_path: Path | None,
    ) -> dict[int, list[str]]:
        """纯 CE 精排：按 CE 分数重排候选池，其余保持原序。"""
        cand = int(p["cand"])
        base, scores_by_qid = self._ce_scores(qids_eval, fit, p, url, model, cache_path)
        out: dict[int, list[str]] = {}
        for i, qid in enumerate(qids_eval):
            docs = base[i]
            head = docs[:cand]
            scores = scores_by_qid[qid]
            order = sorted(range(len(head)), key=lambda j: (-scores[j], j))
            out[qid] = [head[j] for j in order] + docs[cand:]
        return out

    def ceblend_rank(
        self,
        qids_eval: list[int],
        fit: list[int],
        p: dict[str, Any],
        url: str,
        model: str,
        cache_path: Path | None,
    ) -> dict[int, list[str]]:
        """候选池内 z(blend) + μ·z(CE) 融合重排（两者都在同一候选集上归一）。"""
        cand = int(p["cand"])
        mu = float(p["mu"])
        blend = self.blend_scores(
            qids_eval,
            fit,
            {
                "wd": p["wd"],
                "wr": p["wr"],
                "wk": p["wk"],
                "k": p["roc_k"],
                "beta": p["roc_beta"],
                "power": p["roc_power"],
                "knn_k": p["knn_cand_k"],
                "knn_power": p["knn_cand_power"],
            },
        )
        base, scores_by_qid = self._ce_scores(qids_eval, fit, p, url, model, cache_path)
        out: dict[int, list[str]] = {}
        for j, qid in enumerate(qids_eval):
            docs = base[j][:cand]
            cols = [self.id2i[d] for d in docs]
            b = np.asarray(blend[j, cols], dtype=np.float32)
            c = np.asarray(scores_by_qid[qid], dtype=np.float32)
            bz = (b - b.mean()) / (b.std() + 1e-9)
            cz = (c - c.mean()) / (c.std() + 1e-9)
            order = np.argsort(-(bz + mu * cz), kind="stable")
            out[qid] = [docs[i] for i in order] + base[j][cand:]
        return out


    def cez_rank(
        self,
        qids_eval: list[int],
        fit: list[int],
        p: dict[str, Any],
        url: str,
        model: str,
        cache_path: Path | None,
    ) -> dict[int, list[str]]:
        """Dense 候选池上 z(dense 原分数) + μ·z(CE) 融合，不用 CE 名次覆盖。

        与 ``ce_rank`` 共用 ``_ce_scores`` 的候选列表、CE 分数与缓存键（键不含
        mu/workers），因此 A/B 里两个形态只差「怎么合并 CE」，其余逐位相同。
        """
        cand = int(p["cand"])
        mu = float(p["mu"])
        rows = [self.qindex[q] for q in qids_eval]
        dense = self.S[rows]
        base, scores_by_qid = self._ce_scores(qids_eval, fit, p, url, model, cache_path)
        out: dict[int, list[str]] = {}
        for j, qid in enumerate(qids_eval):
            docs = base[j][:cand]
            cols = [self.id2i[d] for d in docs]
            b = np.asarray(dense[j, cols], dtype=np.float32)
            c = np.asarray(scores_by_qid[qid], dtype=np.float32)
            bz = (b - b.mean()) / (b.std() + 1e-9)
            cz = (c - c.mean()) / (c.std() + 1e-9)
            order = np.argsort(-(bz + mu * cz), kind="stable")
            out[qid] = [docs[i] for i in order] + base[j][cand:]
        return out


def _per_query(ranked: dict[int, list[str]], gold: dict[int, set[str]], qids: list[int]) -> list[list[Any]]:
    """把逐题命中位置写进生成物，守卫可据此完整回算所有指标。"""
    out: list[list[Any]] = []
    for qid in qids:
        g = gold[qid]
        positions = [i for i, d in enumerate(ranked.get(qid, [])[:5], 1) if d in g]
        first = next((i for i, d in enumerate(ranked.get(qid, []), 1) if d in g), 0)
        out.append([qid, first, len(g), positions])
    return out


def _verify_metrics(per_query: list[list[Any]]) -> dict[str, float]:
    """守卫侧口径：从逐题位置回算，必须与 stard_eval.evaluate 完全一致。"""
    n = len(per_query)
    out = {"recall@1": 0.0, "recall@5": 0.0, "recall@10": 0.0, "recall@20": 0.0, "mrr": 0.0, "ndcg@5": 0.0}
    for _qid, first, g_size, positions in per_query:
        if first:
            out["mrr"] += 1.0 / first
            for kk in (1, 5, 10, 20):
                if first <= kk:
                    out[f"recall@{kk}"] += 1
        dcg = sum(1.0 / math.log2(rank + 1) for rank in positions)
        ideal = sum(1.0 / math.log2(i + 2) for i in range(min(int(g_size), 5)))
        out["ndcg@5"] += dcg / ideal if ideal else 0.0
    return {key: round(value / n, 6) for key, value in out.items()}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--subset", type=int, default=8000)
    ap.add_argument("--split", choices=("dev", "dev60", "val", "train", "all"), default="dev")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--method", action="append", default=[])
    ap.add_argument("--ce-url", default=CE_URL_DEFAULT)
    ap.add_argument("--ce-model", default=CE_MODEL_DEFAULT)
    ap.add_argument("--embedding-url", default="http://127.0.0.1:1234/v1/embeddings")
    ap.add_argument("--embedding-model", default="text-embedding-bge-m3")
    ap.add_argument("--ce-cache", type=Path)
    ap.add_argument("--write-summary", type=Path)
    ap.add_argument(
        "--append",
        action="store_true",
        help="把本次 report 追加进 --write-summary 的 runs 数组（进仓汇总用）",
    )
    args = ap.parse_args()

    methods = parse_methods(args.method) if args.method else [("dense", {}), ("bm25", {}), ("rrf", {"k": 60})]
    lab = StardLab(subset=args.subset)
    qids_eval, fit = lab.split_qids(args.split, args.limit)
    print(f"[lab] split={args.split} eval_n={len(qids_eval)} fit_n={len(fit)} corpus={len(lab.rows)}", flush=True)

    report: dict[str, Any] = {
        "schema": 2,
        "dataset": "STARD (Su et al., Findings of EMNLP 2024, MIT)",
        "corpus": "STARD 受控子集 8000 条（全部金标法条 + seed=42 干扰文档）",
        "judgement": "精确 docid（qrels），命中定义 = 至少一条相关法条进入 top-k",
        "pool": POOL,
        "splits": {"train": len(lab.train), "dev": len(lab.dev)},
        "eval_split": args.split,
        "eval_n": len(qids_eval),
        "fit_n": len(fit),
        "val_seed": VAL_SEED,
        "val_size": VAL_SIZE,
        "embedding_cache": str(EMB_CACHE.relative_to(ROOT)).replace("\\", "/"),
        "reproduce": " ".join([sys.executable, "evals/stard_recall_lab.py", *sys.argv[1:]]),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "methods": {},
    }

    for name, p in methods:
        t0 = time.perf_counter()
        lab._last_ce_seconds = []
        if name == "dense":
            ranked = {q: lab.dense_rank_all[q] for q in qids_eval}
        elif name == "bm25":
            lab._ensure_bm25()
            ranked = {q: lab.bm25_rank_all[q] for q in qids_eval}
        elif name == "rrf":
            lab._ensure_bm25()
            ranked = {q: lab.rrf_rank_all[q] for q in qids_eval}
        elif name == "knn":
            ranked = lab.knn_rank(qids_eval, fit, p)
        elif name == "rocchio":
            ranked = lab.rocchio_rank(qids_eval, fit, p)
        elif name == "rocchio_knn":
            ranked = lab.rocchio_knn_rank(qids_eval, fit, p)
        elif name == "blend":
            ranked = lab.blend_rank(qids_eval, fit, p)
        elif name == "rrf3":
            ranked = lab.rrf3_rank(qids_eval, fit, p)
        elif name == "docexp":
            ranked = lab.docexp_rank(qids_eval, fit, p)
        elif name == "ltr":
            ranked = lab.ltr_rank(qids_eval, fit, p)
        elif name == "linrank":
            ranked = lab.linrank_rank(qids_eval, fit, p)
        elif name == "zrank":
            ranked = lab.zrank_rank(qids_eval, fit, p)
        elif name == "qexp":
            ranked = lab.qexp_rank(qids_eval, fit, p, args.embedding_url, args.embedding_model)
        elif name == "proj":
            ranked = lab.proj_rank(qids_eval, fit, p)
        elif name == "ce":
            ranked = lab.ce_rank(qids_eval, fit, p, args.ce_url, args.ce_model, args.ce_cache)
        elif name == "ceblend":
            ranked = lab.ceblend_rank(qids_eval, fit, p, args.ce_url, args.ce_model, args.ce_cache)
        elif name == "cez":
            ranked = lab.cez_rank(qids_eval, fit, p, args.ce_url, args.ce_model, args.ce_cache)
        elif name == "hgbce":
            if args.split == "all":
                ranked = lab.hgbce_all_rank(
                    qids_eval, fit, p, args.ce_url, args.ce_model, args.ce_cache
                )
            else:
                ranked = lab.hgbce_rank(
                    qids_eval, fit, p, args.ce_url, args.ce_model, args.ce_cache
                )
        else:  # pragma: no cover - parse_methods 已拦截
            raise SystemExit(name)

        label = name if not p else f"{name}:{','.join(f'{k}={v}' for k, v in p.items())}"
        dev_qids = [q for q in qids_eval if q in lab.dev]
        train_qids = [q for q in qids_eval if q in lab.train]
        metrics: dict[str, Any] = {}
        for split_name, sq in (("dev", dev_qids), ("train", train_qids), ("all", qids_eval)):
            if not sq:
                continue
            metrics[split_name] = evaluate(ranked, lab.gold, sq)
        pq = _per_query(ranked, lab.gold, qids_eval)
        entry = {
            "params": p,
            "metrics": metrics,
            "per_query": pq,
            "verify_all": _verify_metrics(pq),
            "seconds": round(time.perf_counter() - t0, 2),
        }
        if name == "hgbce" and args.split == "all":
            entry["evaluation_protocol"] = lab._hgbce_evaluation_protocol
        if name in ("ce", "ceblend", "cez", "hgbce") and lab._last_ce_seconds:
            secs = sorted(lab._last_ce_seconds)
            entry["ce_latency"] = {
                "n": len(secs),
                "p50_s": round(floor_rank_quantile(secs, 0.50), 4),
                "p95_s": round(floor_rank_quantile(secs, 0.95), 4),
                "max_s": round(secs[-1], 4),
                "note": "单题上游 /rerank wall time；workers=1，无排队",
            }
        report["methods"][label] = entry
        shown = metrics.get("dev") or metrics.get("all")
        print(
            f"[{label[:66]:66s}] R@5={shown['recall@5']:.4f} R@10={shown['recall@10']:.4f} "
            f"MRR={shown['mrr']:.4f} NDCG@5={shown['ndcg@5']:.4f} ({entry['seconds']}s)",
            flush=True,
        )

    if args.write_summary:
        args.write_summary.parent.mkdir(parents=True, exist_ok=True)
        if args.append:
            runs: list[dict[str, Any]] = []
            if args.write_summary.exists():
                previous = json.loads(args.write_summary.read_text(encoding="utf-8"))
                runs = list(previous.get("runs", [previous]))
            runs.append(report)
            payload: dict[str, Any] = {
                "schema": 3,
                "dataset": report["dataset"],
                "corpus": report["corpus"],
                "judgement": report["judgement"],
                "pool": POOL,
                "splits": report["splits"],
                "runs": runs,
            }
        else:
            payload = report
        args.write_summary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"[saved] {args.write_summary} (runs={len(payload.get('runs', [])) or 1})", flush=True)
    else:
        print(json.dumps(report["methods"], ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
