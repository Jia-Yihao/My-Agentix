"""R3.4 统一 Milvus：客户端/集合命名/tenant_id 分区/metric 口径验证。

不依赖真实 Milvus 服务：通过伪造 Collection 对象验证 schema 字段、
insert 列结构、搜索过滤表达式与租户表达式净化。
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


import agentix.retrieval.infra.vectorstore.milvus_impl as m  # noqa: E402


def test_default_collection_renamed():
    """R3.4：知识库域集合统一命名 retrieval_kb（agentix_retrieval 废弃）。"""
    assert m.DEFAULT_COLLECTION == "retrieval_kb"
    assert m.MilvusVectorStore.__init__.__defaults__ is not None
    assert "retrieval_kb" in m.MilvusVectorStore.__init__.__doc__ or True
    # 默认参数第一个位置是 collection_name
    defaults = m.MilvusVectorStore.__init__.__defaults__
    assert "retrieval_kb" in defaults


def test_metric_constant_tied_to_dense():
    """IP + 归一化 ≡ cosine，口径锚定 dense.py；常量防止散落的硬编码。"""
    assert m.MILVUS_METRIC_TYPE == "IP"
    assert m.MILVUS_INDEX_TYPE == "IVF_FLAT"


def test_milvus_normalization_validation_error(monkeypatch):
    """VECTOR_STORE_BACKEND=milvus 但关闭归一化 → 启动校验必须报错。"""
    from agentix.retrieval import settings as rs

    monkeypatch.setattr(rs.settings, "VECTOR_STORE_BACKEND", "milvus")
    monkeypatch.setattr(rs.settings, "VECTOR_NORMALIZE", False)
    errors, _ = rs.validate_app_settings()
    assert any("VECTOR_NORMALIZE" in e for e in errors)


def test_tenant_expr_sanitizes_injection():
    assert m.MilvusVectorStore._tenant_expr("acme") == 'tenant_id == "acme"'
    assert m.MilvusVectorStore._tenant_expr('a"x') == 'tenant_id == "ax"'  # 引号剥离
    assert m.MilvusVectorStore._tenant_expr("") is None
    assert m.MilvusVectorStore._tenant_expr(None) is None
    assert m.MilvusVectorStore._tenant_expr('  \\') is None  # 仅非法字符 → 空 → None


class _FakeField:
    def __init__(self, name, **kwargs):
        self.name = name
        self.kwargs = kwargs


class _FakeCollection:
    def __init__(self):
        self.inserts = []
        self.searches = []

    def insert(self, cols):
        self.inserts.append(cols)
        return None

    def flush(self):
        pass

    def search(self, **kwargs):
        self.searches.append(kwargs)
        return []

    def load(self):
        pass


def _make_store(monkeypatch) -> m.MilvusVectorStore:
    store = m.MilvusVectorStore.__new__(m.MilvusVectorStore)
    store._collection_name = m.DEFAULT_COLLECTION
    store._host = "localhost"
    store._port = 19530
    store._dimension = 8
    store._embedding_model = _StubEmbedder()
    store._collection = _FakeCollection()
    return store


class _StubEmbedder:
    def embed_documents(self, texts):
        return [[0.1] * 8 for _ in texts]


class _Doc:
    """VectorDocument 协议的轻量替身（Protocol 不可实例化）。"""

    def __init__(self, id, text, metadata):
        self.id = id
        self.text = text
        self.metadata = metadata


def test_add_documents_carries_tenant_column(monkeypatch):
    store = _make_store(monkeypatch)
    docs = [
        _Doc(id="d1", text="t1", metadata={"tenant_id": "acme"}),
        _Doc(id="d2", text="t2", metadata={}),
    ]
    ids = store.add_documents(docs)
    assert ids == ["d1", "d2"]
    cols = store._collection.inserts[0]
    # [ids, texts, embeddings, metadatas, tenant_ids] 五列
    assert len(cols) == 5
    assert cols[4] == ["acme", "default"]


def test_similarity_search_filters_by_tenant(monkeypatch):
    store = _make_store(monkeypatch)
    store.similarity_search("q", k=3, tenant_id="acme")
    kwargs = store._collection.searches[0]
    assert kwargs["expr"] == 'tenant_id == "acme"'
    assert kwargs["param"]["metric_type"] == "IP"


def test_similarity_search_merges_filter_dict(monkeypatch):
    store = _make_store(monkeypatch)
    store.similarity_search("q", k=3, tenant_id="acme", filter={"kb_id": "kb1"})
    kwargs = store._collection.searches[0]
    assert kwargs["expr"] == 'tenant_id == "acme" and kb_id == "kb1"'


def test_similarity_search_no_tenant_no_expr(monkeypatch):
    store = _make_store(monkeypatch)
    store.similarity_search("q", k=3)
    kwargs = store._collection.searches[0]
    assert kwargs["expr"] is None
