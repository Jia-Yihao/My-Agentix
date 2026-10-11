"""R2-2：法条效力过滤测试。

依据：《技术变更与架构演进指南 v2.0》§2.2.7①——数据模型四字段
``effective_status/effective_date/expire_date/replaced_by``；检索默认只查现行有效；
答案标注"检索截至日期"；sources 带 status。

验收（本片覆盖向量检索路径）：
- 插入 expired 条文 → 检索不返回（后过滤语义，三后端一致）；
- 存量无效力字段数据（默认 in_force）零误伤；
- 答案尾部出现"检索截至"标注；hit_chunks 携带 effective_status。
"""

from __future__ import annotations

from datetime import date, timedelta

from langchain_core.documents import Document as LCDocument

from agentix.domain.policies.legal_effectiveness import (
    STATUS_EXPIRED,
    STATUS_IN_FORCE,
    STATUS_PENDING,
    STATUS_REPEALED,
    append_retrieved_asof,
    attach_effective_status,
    effective_status_of,
    filter_in_force,
    format_retrieved_asof,
    is_in_force,
)
from agentix.retrieval.usecases.chat_service import (
    _resolve_retrieval_fetch_k,
    hit_chunks_from_docs,
)

_TODAY = date(2026, 9, 21)
_FUTURE = _TODAY + timedelta(days=365)
_PAST = _TODAY - timedelta(days=365)


class _FakeDoc:
    def __init__(self, metadata: dict):
        self.metadata = metadata


# ---------------------------------------------------------------- 状态判定


def test_missing_fields_defaults_in_force():
    """存量 1953 条无效力字段数据 → 默认现行有效（零误伤）。"""
    assert effective_status_of({}) == STATUS_IN_FORCE
    assert effective_status_of(None) == STATUS_IN_FORCE
    assert is_in_force({"source": "某法", "chunk_id": "x"}) is True


def test_explicit_status_expired_and_repealed():
    assert effective_status_of({"effective_status": "expired"}, now=_TODAY) == STATUS_EXPIRED
    assert effective_status_of({"effective_status": "repealed"}, now=_TODAY) == STATUS_REPEALED
    assert effective_status_of({"effective_status": "已废止"}, now=_TODAY) == STATUS_REPEALED
    assert effective_status_of({"effective_status": "已失效"}, now=_TODAY) == STATUS_EXPIRED
    assert is_in_force({"effective_status": "expired"}, now=_TODAY) is False


def test_explicit_status_in_force():
    assert effective_status_of({"effective_status": "in_force"}, now=_TODAY) == STATUS_IN_FORCE
    assert effective_status_of({"effective_status": "现行有效"}, now=_TODAY) == STATUS_IN_FORCE


def test_future_effective_date_is_pending():
    meta = {"effective_date": _FUTURE.isoformat()}
    assert effective_status_of(meta, now=_TODAY) == STATUS_PENDING
    assert is_in_force(meta, now=_TODAY) is False


def test_past_expire_date_is_expired():
    meta = {"expire_date": _PAST.isoformat()}
    assert effective_status_of(meta, now=_TODAY) == STATUS_EXPIRED
    assert is_in_force(meta, now=_TODAY) is False


def test_expire_date_is_inclusive():
    """失效当天仍有效（"有效期至 X 日"语义）。"""
    meta = {"expire_date": _TODAY.isoformat()}
    assert is_in_force(meta, now=_TODAY) is True


def test_ingest_side_aliases_recognized():
    """与 kb_ingest legal_metadata.py 字段对齐：legal_effective_from/to。"""
    assert is_in_force({"legal_effective_to": _PAST.isoformat()}, now=_TODAY) is False
    assert effective_status_of({"legal_effective_from": _FUTURE.isoformat()}, now=_TODAY) == STATUS_PENDING


def test_bad_date_fail_open():
    """日期解析失败 → 视为有效（法律域宁可漏报不误报）。"""
    assert is_in_force({"expire_date": "不是日期"}, now=_TODAY) is True
    assert is_in_force({"effective_date": "2021-13-99"}, now=_TODAY) is True


def test_both_dates_valid_window():
    meta = {"effective_date": _PAST.isoformat(), "expire_date": _FUTURE.isoformat()}
    assert is_in_force(meta, now=_TODAY) is True


# ---------------------------------------------------------------- 后过滤


def test_filter_in_force_drops_expired():
    pairs = [
        (_FakeDoc({"chunk_id": "valid"}), 0.9),
        (_FakeDoc({"effective_status": "expired"}), 0.95),
        (_FakeDoc({"expire_date": _PAST.isoformat()}), 0.8),
        (_FakeDoc({}), 0.7),  # 存量默认有效
    ]

    kept = filter_in_force(pairs, now=_TODAY)

    assert [d.metadata["chunk_id"] for d, _ in kept if "chunk_id" in (d.metadata or {})] == ["valid"]
    # 顺序保持 + 存量保留
    assert len(kept) == 2


def test_filter_in_force_disabled_passthrough():
    pairs = [(_FakeDoc({"effective_status": "expired"}), 0.9)]
    assert filter_in_force(pairs, now=_TODAY, enabled=False) is pairs


def test_filter_in_force_bad_metadata_fail_open():
    doc = _FakeDoc(None)  # metadata None → 视为有效
    kept = filter_in_force([(doc, 0.5)], now=_TODAY)
    assert len(kept) == 1


# ---------------------------------------------------------------- 检索管道集成


class _FakeVectorStore:
    """模拟带 similarity_search_with_score 的向量后端（返回固定候选）。"""

    def __init__(self, pairs):
        self._pairs = pairs

    def similarity_search_with_score(self, query, k=5, filter=None, fetch_k=None):
        return self._pairs[: fetch_k or k]


def test_search_pipeline_excludes_expired(monkeypatch):
    """验收核心：插入 expired 条文 → 查询不返回；有效期条文正常返回。"""
    from agentix.retrieval.settings import settings as rs

    monkeypatch.setattr(rs, "EFFECTIVENESS_FILTER_ENABLED", True, raising=False)
    expired = LCDocument(page_content="已废止条文", metadata={"effective_status": "expired"})
    valid = LCDocument(page_content="现行条文", metadata={"chunk_id": "v1"})
    store = _FakeVectorStore([(expired, 0.9), (valid, 0.8)])

    from agentix.retrieval.usecases.chat_service import _similarity_search_with_fetch

    docs = _similarity_search_with_fetch(store, "查询", search_k=2, metadata_filter=None, fetch_k=10)

    assert len(docs) == 1
    assert docs[0].page_content == "现行条文"


def test_search_pipeline_legacy_docs_not_dropped(monkeypatch):
    from agentix.retrieval.settings import settings as rs

    monkeypatch.setattr(rs, "EFFECTIVENESS_FILTER_ENABLED", True, raising=False)
    legacy = LCDocument(page_content="存量法条", metadata={"source": "某法"})
    store = _FakeVectorStore([(legacy, 0.9)])

    from agentix.retrieval.usecases.chat_service import _similarity_search_with_fetch

    docs = _similarity_search_with_fetch(store, "查询", search_k=5, metadata_filter=None, fetch_k=10)
    assert len(docs) == 1


def test_fetch_k_post_filtered_bounded():
    """效力后过滤开启时，即使无 metadata_filter 也取有界 fetch_k（补偿召回）。"""

    class _Idx:
        ntotal = 1000

    class _VS:
        index = _Idx()

    base = _resolve_retrieval_fetch_k(_VS(), 5, None, post_filtered=False)
    bounded = _resolve_retrieval_fetch_k(_VS(), 5, None, post_filtered=True)
    assert base == max(5 * 4, 20)
    assert bounded >= base


# ---------------------------------------------------------------- 标注与 sources


def test_format_and_append_asof():
    line = format_retrieved_asof(_TODAY)
    assert line == "检索截至 2026-09-21"

    out = append_retrieved_asof("答案主体。", now=_TODAY)
    assert out.startswith("答案主体。")
    assert "检索截至 2026-09-21" in out


def test_hit_chunks_carry_effective_status():
    docs = [
        LCDocument(page_content="现行", metadata={"chunk_id": "a", "_retrieval_score": 0.9, "source": "A法"}),
        LCDocument(
            page_content="废止", metadata={"chunk_id": "b", "_retrieval_score": 0.8, "effective_status": "expired"}
        ),
    ]

    hits = hit_chunks_from_docs(docs)

    assert hits[0]["effective_status"] == STATUS_IN_FORCE
    assert hits[1]["effective_status"] == STATUS_EXPIRED


def test_attach_effective_status_with_replaced_by():
    hit = attach_effective_status({"chunk_id": "x"}, {"effective_status": "expired", "replaced_by": "民法典"})
    assert hit["effective_status"] == STATUS_EXPIRED
    assert hit["replaced_by"] == "民法典"

    hit2 = attach_effective_status({"chunk_id": "y"}, {})
    assert "replaced_by" not in hit2
