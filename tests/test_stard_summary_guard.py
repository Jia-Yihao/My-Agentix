"""STARD 进仓汇总 ↔ README §15.3 / evals/README §4.9 的一致性守卫。

只守文档与生成物之间的边，不重算任何指标。
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICAL = REPO_ROOT / "docs/evals/stard-lexical-summary.json"
SEMANTIC = REPO_ROOT / "docs/evals/stard-semantic-summary.json"
README = REPO_ROOT / "README.md"
EVALS_README = REPO_ROOT / "evals/README.md"
ENV_EXAMPLE = REPO_ROOT / ".env.example"


def _section() -> str:
    text = README.read_text(encoding="utf-8")
    start = text.index("### 15.3")
    rest = text[start:]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _evals_section() -> str:
    text = EVALS_README.read_text(encoding="utf-8")
    start = text.index("### 4.9 STARD 检索评测")
    rest = text[start:]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_summaries_share_dataset_and_splits():
    for path in (LEXICAL, SEMANTIC):
        s = _load(path)
        assert s["schema"] == 1
        assert "STARD" in s["dataset"]
        assert s["judgement"].startswith("精确 docid")
        assert s["splits"] == {"train": 1235, "dev": 308}
        assert s["pool"] == 100


def test_corpus_boundary_is_explicit():
    assert "55348" in _load(LEXICAL)["corpus"]
    assert "8000" in _load(SEMANTIC)["corpus"]
    sec = _section()
    assert "只用于同一子集内的方法间比较" in sec
    assert "两行不可互读" in sec


def test_published_numbers_match_summaries():
    """§15.3 表里每个 dev / 全量 R@5 都能由汇总回算，手改一边就红。"""
    sec = _section()
    checks = [
        (LEXICAL, "bm25", "dev", "recall@5"),
        (LEXICAL, "bm25", "dev", "recall@10"),
        (LEXICAL, "bm25", "dev", "mrr"),
        (LEXICAL, "bm25", "dev", "ndcg@5"),
        (LEXICAL, "bm25", "all", "recall@5"),
        (SEMANTIC, "bm25", "dev", "recall@5"),
        (SEMANTIC, "bm25", "all", "recall@5"),
        (SEMANTIC, "dense", "dev", "recall@5"),
        (SEMANTIC, "dense", "dev", "mrr"),
        (SEMANTIC, "dense", "all", "recall@5"),
        (SEMANTIC, "rrf", "dev", "recall@5"),
        (SEMANTIC, "rrf", "all", "recall@5"),
    ]
    missing = []
    for path, col, split, key in checks:
        value = _load(path)["columns"][col][split][key]
        text = f"{value:.4f}"
        if text not in sec:
            missing.append(f"{path.name}:{col}/{split}/{key}={text}")
    assert not missing, "§15.3 与进仓汇总不一致：\n" + "\n".join(missing)


def test_current_channel_conclusion_is_stated():
    sec = _section()
    assert "默认检索保持 Dense 单通道" in sec
    assert "RETRIEVAL_DENSE_RERANK_ENABLED" in sec
    assert "+12.98" in sec


def test_evals_readme_table_matches_summaries():
    """evals/README §4.9 的四行数字同样由进仓汇总回算。"""
    sec = _evals_section()
    checks = [
        (LEXICAL, "bm25", "recall@5"),
        (LEXICAL, "bm25", "mrr"),
        (SEMANTIC, "bm25", "recall@5"),
        (SEMANTIC, "bm25", "mrr"),
        (SEMANTIC, "dense", "recall@5"),
        (SEMANTIC, "dense", "mrr"),
        (SEMANTIC, "rrf", "recall@5"),
        (SEMANTIC, "rrf", "mrr"),
    ]
    missing = []
    for path, col, key in checks:
        value = _load(path)["columns"][col]["dev"][key]
        text = f"{value:.4f}"
        if text not in sec:
            missing.append(f"{path.name}:{col}/{key}={text}")
    assert not missing, "§4.9 与进仓汇总不一致：\n" + "\n".join(missing)


def test_evals_readme_points_at_current_section():
    text = EVALS_README.read_text(encoding="utf-8")
    assert "§4.9 / README §15.3" in text
    assert text.count("### 4.8 ") == 1
    assert text.count("### 4.9 ") == 1
    assert "§4.8 聚合与退出契约键" in text


def test_current_metric_pointer_and_no_stale_numbers():
    text = README.read_text(encoding="utf-8")
    assert "当前检索质量口径是 §15.3" in text
    assert "### 15.1" in text and "历史读数" in text.split("### 15.1", 1)[1][:120]
    assert "### 15.2" in text and "历史读数" in text.split("### 15.2", 1)[1][:120]
    env = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "0.7283" not in env
    assert "18/23" not in env
    assert "RETRIEVAL_DENSE_RERANK_ENABLED=false" in env
    limits = (REPO_ROOT / "docs/已知限制.md").read_text(encoding="utf-8")
    assert "检索质量当前口径是 STARD 公开集" in limits
    assert "README §15.3 与 evals/README §4.9" in limits
