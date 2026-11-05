"""Dense 单通道候选池精排：默认关闭、放大候选、截断、fail-open。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentix.retrieval.settings import settings as rset
from agentix.retrieval.usecases import dense_rerank

REPO_ROOT = Path(__file__).resolve().parents[1]
RERANK_PATH = "agentix.retrieval.infra.rag.rerank.rerank_documents_with_status"


def test_flag_defaults_off():
    assert rset.RETRIEVAL_DENSE_RERANK_ENABLED is False


def test_config_key_is_declared_in_both_places():
    env = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "RETRIEVAL_DENSE_RERANK_ENABLED=false" in env
    src = (REPO_ROOT / "agentix/retrieval/settings.py").read_text(encoding="utf-8")
    assert '"RETRIEVAL_DENSE_RERANK_ENABLED", "false"' in src


def test_cache_pool_is_isolated():
    src = (REPO_ROOT / "agentix/retrieval/usecases/chat_service.py").read_text(encoding="utf-8")
    assert 'key += "|d1"' in src
    assert "RETRIEVAL_RERANK_CANDIDATES" in src


def test_candidates_not_larger_than_search_k_skip_rerank(monkeypatch):
    calls: list[str] = []

    async def fake(*_args, **_kwargs):
        calls.append("called")
        return [], True

    monkeypatch.setattr(RERANK_PATH, fake)
    docs = ["a", "b"]
    result = asyncio.run(dense_rerank.rerank_dense_candidates("q", docs, 5))
    assert result == docs
    assert not calls


@pytest.mark.parametrize("applied", [True, False])
def test_rerank_truncates_to_search_k(monkeypatch, applied):
    docs = [f"d{i}" for i in range(20)]

    async def fake(question, candidates, model=None):
        assert question == "原始问题"
        assert len(candidates) == 20
        return list(reversed(candidates)), applied

    monkeypatch.setattr(RERANK_PATH, fake)
    result = asyncio.run(dense_rerank.rerank_dense_candidates("原始问题", docs, 5))
    expected = (list(reversed(docs)) if applied else docs)[:5]
    assert result == expected


def test_rerank_exception_is_fail_open(monkeypatch):
    docs = [f"d{i}" for i in range(20)]

    async def broken(*_args, **_kwargs):
        raise RuntimeError("reranker unavailable")

    monkeypatch.setattr(RERANK_PATH, broken)
    assert asyncio.run(dense_rerank.rerank_dense_candidates("q", docs, 5)) == docs[:5]
