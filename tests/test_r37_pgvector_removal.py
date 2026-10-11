"""R3.7：kb_ingest pgvector → Milvus 迁移守卫。

验证点（不依赖真实 Milvus，全部走 fake Collection）：
1. `_doc_id` 确定性主键（sha1(source_table|source_id)[:32]）；
2. `_l2_normalize` 单位向量（IP ≡ cosine 的前提）；
3. `_escape` expr 转义；
4. upsert 幂等（先删后插）与列结构（8 列含 tenant_id partition key）；
5. 增量模式按 content_hash 跳过未变化条目；
6. search 过滤表达式下推（source_tables / effective_on / authority）；
7. company 模糊过滤：超量召回 k*3 + Python 子串过滤；
8. threshold / metric 校验；
9. delete_records 按主键精确删除；
10. VectorStoreWriter 兼容 shim 继承关系。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Dict, List

import pytest

from kb_service.core.config import Config
from kb_service.services.milvus_store import (
    COLLECTION_NAME,
    MilvusLegalStore,
    _doc_id,
    _escape,
    _l2_normalize,
)
from kb_service.services.vector_store import VectorStoreWriter


# --------------------------------------------------------------------- #
# Fake 基建
# --------------------------------------------------------------------- #
_IN_EXPR = re.compile(r'"([^"]*)"')


class _FakeEntity:
    def __init__(self, row_data: Dict):
        self._row_data = row_data


class _FakeHit:
    def __init__(self, hit_id: str, score: float, row_data: Dict):
        self.id = hit_id
        self.score = score
        self.entity = _FakeEntity(row_data)


class _FakeCollection:
    """只实现 milvus_store 用到的最小接口：delete/insert/flush/query/search。"""

    def __init__(self):
        self.rows: Dict[str, Dict] = {}
        self.delete_exprs: List[str] = []
        self.insert_calls: List[List[list]] = []
        self.query_calls: List[Dict] = []
        self.search_calls: List[Dict] = []
        self._search_result: List[_FakeHit] = []

    # -- 写入 ---------------------------------------------------------- #
    def delete(self, expr: str):
        self.delete_exprs.append(expr)
        for pk in _IN_EXPR.findall(expr):
            self.rows.pop(pk, None)

    def insert(self, data: List[list]):
        self.insert_calls.append(data)
        columns = ("id", "source_table", "source_id", "content", "embedding", "metadata", "content_hash", "tenant_id")
        for row in zip(*data):
            record = dict(zip(columns, row))
            self.rows[record["id"]] = record

    def flush(self):
        pass

    # -- 查询 ---------------------------------------------------------- #
    def query(self, expr: str, output_fields: List[str], limit: int = None):
        self.query_calls.append({"expr": expr, "output_fields": output_fields, "limit": limit})
        fields = ["id", *output_fields]
        if expr.startswith("id in"):
            out = []
            for pk in _IN_EXPR.findall(expr):
                row = self.rows.get(pk)
                if row is None:
                    continue
                out.append({k: row.get(k) for k in fields})
            return out
        m = re.search(r'source_table == "([^"]*)"', expr)
        if m:
            return [
                {k: row.get(k) for k in fields}
                for row in self.rows.values()
                if row.get("source_table") == m.group(1)
            ]
        return []

    def search(self, data, anns_field, param, limit, expr=None, output_fields=None):
        self.search_calls.append(
            {
                "anns_field": anns_field,
                "param": param,
                "limit": limit,
                "expr": expr,
                "output_fields": output_fields,
            }
        )
        # 简化模型：直接返回预置 hits（按 limit 截断）
        return [[h for h in self._search_result[:limit]]]


class _FakeEmbedding:
    """duck-typed EmbeddingClient：固定 4 维向量。"""

    dimension = 4

    def embed_texts(self, texts):
        return [[0.5, 0.5, 0.5, 0.5] for _ in texts]


def _make_store() -> tuple:
    config = Config()
    store = MilvusLegalStore(config, embedding_client=_FakeEmbedding())
    fake = _FakeCollection()
    store._connect = lambda: fake  # 跳过 pymilvus 连接
    return store, fake


def _item(table="laws", sid="1", content="善意取得的构成要件", original=None):
    return {
        "source_table": table,
        "source_id": sid,
        "rewritten_content": content,
        "original_data": original or {"company_name": "某某公司", "title": "民法典条款"},
    }


# --------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------- #
def test_doc_id_deterministic_and_shape():
    a = _doc_id("laws", "1")
    b = _doc_id("laws", "1")
    c = _doc_id("laws", "2")
    d = _doc_id("regs", "1")
    assert a == b
    assert len(a) == 32
    int(a, 16)  # hex
    assert a != c and a != d


def test_l2_normalize():
    vec = _l2_normalize([3.0, 4.0])
    assert sum(x * x for x in vec) == pytest.approx(1.0)
    assert _l2_normalize([0.0, 0.0]) == [0.0, 0.0]  # 零向量原样返回


def test_escape():
    assert _escape('say "hi"') == 'say \\"hi\\"'
    assert _escape("back\\slash") == "back\\\\slash"


# --------------------------------------------------------------------- #
# upsert / 增量
# --------------------------------------------------------------------- #
def test_upsert_idempotent_same_pk():
    store, fake = _make_store()
    assert store.upsert([_item()]) == 1
    assert len(fake.rows) == 1
    assert store.upsert([_item()]) == 1
    assert len(fake.rows) == 1  # 主键相同 → 覆盖而非新增
    # 每批先删后插
    assert fake.delete_exprs, "应先按主键 delete 再 insert"


def test_upsert_column_layout():
    store, fake = _make_store()
    store.upsert([_item(sid="7")])
    data = fake.insert_calls[-1]
    assert len(data) == 8  # id/source_table/source_id/content/embedding/metadata/content_hash/tenant_id
    pk = _doc_id("laws", "7")
    row = fake.rows[pk]
    assert row["id"] == pk
    assert row["source_table"] == "laws"
    assert row["tenant_id"] == "kb_ingest"
    assert row["metadata"]["company_name"] == "某某公司"
    assert row["content_hash"]
    assert sum(x * x for x in row["embedding"]) == pytest.approx(1.0)  # L2 归一化


def test_upsert_incremental_skips_unchanged():
    store, fake = _make_store()
    store.upsert([_item(sid="1"), _item(sid="2")])
    before = len(fake.insert_calls)
    assert store.upsert([_item(sid="1"), _item(sid="2")], incremental=True) == 0
    assert len(fake.insert_calls) == before  # 全部未变化 → 无写入


def test_upsert_incremental_detects_change():
    store, fake = _make_store()
    store.upsert([_item(sid="1")])
    changed = _item(sid="1", original={"company_name": "某某公司", "title": "修改后的标题"})
    assert store.upsert([changed], incremental=True) == 1


# --------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------- #
def _seed_hits(fake: _FakeCollection, count=3):
    hits = []
    for i in range(count):
        row = {
            "source_table": "laws",
            "source_id": str(i),
            "content": f"条款 {i}",
            "metadata": {"company_name": f"公司{i}", "legal_effective_from": "2020-01-01"},
        }
        fake.rows[_doc_id("laws", str(i))] = {"id": _doc_id("laws", str(i)), **row}
        hits.append(_FakeHit(_doc_id("laws", str(i)), 0.9 - i * 0.1, row))
    fake._search_result = hits


def test_search_builds_filter_expr():
    store, fake = _make_store()
    _seed_hits(fake)
    store.search_similar(
        query_embedding=[1.0, 0.0, 0.0, 0.0],
        top_k=5,
        source_tables=["laws", "regs"],
        effective_on=date(2021, 6, 1),
        source_authority_level="法律",
    )
    call = fake.search_calls[-1]
    assert call["anns_field"] == "embedding"
    assert call["param"]["metric_type"] == "IP"
    expr = call["expr"]
    assert 'source_table in ["laws", "regs"]' in expr
    assert "metadata['legal_effective_from']" in expr and '<= "2021-06-01"' in expr
    assert "metadata['legal_effective_to']" in expr and '>= "2021-06-01"' in expr
    assert "metadata['source_authority_level'] == \"法律\"" in expr


def test_search_company_overfetch_and_substring():
    store, fake = _make_store()
    _seed_hits(fake, count=6)
    store.search_similar(
        query_embedding=[1.0, 0.0, 0.0, 0.0],
        top_k=2,
        company_filter="公司3",
    )
    call = fake.search_calls[-1]
    assert call["limit"] == 6  # k*3 超量召回


def test_search_threshold_and_result_shape():
    store, fake = _make_store()
    _seed_hits(fake, count=3)
    hits = store.search_similar(
        query_embedding=[1.0, 0.0, 0.0, 0.0],
        top_k=3,
        threshold=0.85,
    )
    # hits 分数 0.9/0.8/0.7 → threshold=0.85 只留第一条
    assert len(hits) == 1
    item = hits[0]
    for key in ("id", "source_table", "source_id", "metadata", "content", "similarity", "distance", "score", "document_id", "source"):
        assert key in item
    assert item["score"] == pytest.approx(item["similarity"])
    assert item["distance"] == pytest.approx(1.0 - item["similarity"])


def test_search_invalid_metric_raises():
    store, _ = _make_store()
    with pytest.raises(ValueError):
        store.search_similar(query_embedding=[1.0, 0.0, 0.0, 0.0], metric="dot")


# --------------------------------------------------------------------- #
# 运维通道
# --------------------------------------------------------------------- #
def test_delete_records_by_pk():
    store, fake = _make_store()
    store.upsert([_item(sid="1"), _item(sid="2")])
    assert len(fake.rows) == 2
    store.delete_records("laws", ["1"])
    assert _doc_id("laws", "1") not in fake.rows
    assert _doc_id("laws", "2") in fake.rows


def test_query_records_company_substring():
    store, fake = _make_store()
    store.upsert([_item(sid="1"), _item(sid="2", original={"company_name": "另一家集团"})])
    rows = store.query_records(source_table="laws", company_name="某")
    assert len(rows) == 1
    assert rows[0]["source_id"] == "1"
    assert rows[0]["metadata"]["company_name"] == "某某公司"


# --------------------------------------------------------------------- #
# 兼容层
# --------------------------------------------------------------------- #
def test_vector_store_writer_is_milvus_store():
    assert issubclass(VectorStoreWriter, MilvusLegalStore)
    writer = VectorStoreWriter(Config(), embedding_client=_FakeEmbedding())
    fake = _FakeCollection()
    writer._connect = lambda: fake
    assert writer.upsert([_item()], None, incremental=False) == 1


def test_collection_name_default():
    store, _ = _make_store()
    assert store.collection_name == COLLECTION_NAME
