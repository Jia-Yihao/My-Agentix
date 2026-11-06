"""从 STARD train qrels + 缓存向量构建生产侧 query 相关性记忆快照。

只使用官方 train 1,235 题的 qrels 与 query 向量；dev 308 不参与任何记忆构建。
产物是 ``.tmp/stard/query_memory.npz``（已 gitignore），字段契约见
``agentix.retrieval.domain.policies.query_memory.load_query_memory``。

复现：
  .venv/Scripts/python.exe evals/stard_build_query_memory.py
  .venv/Scripts/python.exe evals/stard_build_query_memory.py --output D:/tmp/qm.npz
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.stard_eval import DATA, load, subset_rows  # noqa: E402

EMB_CACHE = DATA / "emb_8000_text-embedding-bge-m3.npz"


def build(output: Path, subset: int = 8000) -> dict:
    rows, _qs, train, dev = load()
    subset_docs = subset_rows(rows, {**train, **dev}, subset)
    doc_ids = [r[0] for r in subset_docs]
    doc_pos = {d: i for i, d in enumerate(doc_ids)}

    with np.load(EMB_CACHE) as z:
        D = np.asarray(z["doc"], dtype=np.float32)
        Q = np.asarray(z["q"], dtype=np.float32)
        qids = [int(x) for x in z["qids"]]
    if D.shape[0] != len(doc_ids):
        raise SystemExit("embedding 缓存与受控子集不一致，删除 .tmp/stard/emb_8000_*.npz 重跑")
    qpos = {q: i for i, q in enumerate(qids)}

    memory_qids = sorted(train)
    memory_q = np.stack([Q[qpos[q]] for q in memory_qids]).astype(np.float32)
    ptr = [0]
    flat: list[int] = []
    for q in memory_qids:
        for d in sorted(train[q]):
            if d in doc_pos:
                flat.append(doc_pos[d])
        ptr.append(len(flat))

    output.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        output,
        memory_q=memory_q.astype(np.float16),
        memory_doc_ptr=np.asarray(ptr, dtype=np.int64),
        memory_doc_flat=np.asarray(flat, dtype=np.int64),
        doc_ids=np.asarray(doc_ids, dtype=np.str_),
        doc_vectors=D.astype(np.float16),
    )
    stats = {
        "output": str(output),
        "memory_queries": len(memory_qids),
        "memory_pairs": len(flat),
        "corpus_docs": len(doc_ids),
        "dim": int(D.shape[1]),
        "dev_queries_excluded": len(dev),
    }
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", type=Path, default=DATA / "query_memory.npz")
    ap.add_argument("--subset", type=int, default=8000)
    args = ap.parse_args()
    stats = build(args.output, args.subset)
    for key, value in stats.items():
        print(f"[{key}] {value}", flush=True)


if __name__ == "__main__":
    main()
