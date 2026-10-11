"""
Milvus 向量库实现

使用 pymilvus 官方客户端，支持分布式向量库操作。

R3.4 统一口径：
- 集合命名：`retrieval_kb`（知识库数据）；法条向量集合 `legal` 由 kb_ingest 侧
  使用（MILVUS_COLLECTION 配置），两集合共存互不污染。
- 租户隔离：schema 携带 tenant_id 字段（partition_key），检索强制租户过滤，
  禁止跨租户复用同一分区。
- metric 口径：IP（内积）+ 归一化向量 ≡ cosine similarity，以
  retrieval/infra/rag/dense.py 的相似度口径为准（VECTOR_NORMALIZE=true 前置）。
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from agentix.retrieval.infra.vectorstore.base import VectorDocument, VectorStore, VectorStoreFactory

logger = logging.getLogger(__name__)

# R3.4：集合命名规范（知识库域唯一集合）
DEFAULT_COLLECTION = "retrieval_kb"
# R3.4：metric 与 dense.py 口径绑定——向量归一化（VECTOR_NORMALIZE=true）时
# IP 内积等价 cosine similarity；若关闭归一化，IP 语义漂移，启动校验会告警。
MILVUS_METRIC_TYPE = "IP"
MILVUS_INDEX_TYPE = "IVF_FLAT"


class MilvusVectorStore(VectorStore):
    """Milvus 向量库实现"""

    def __init__(
        self,
        collection_name: str = DEFAULT_COLLECTION,
        host: str = "localhost",
        port: int = 19530,
        embedding_model: Any = None,
        dimension: int = 1024,
        **kwargs,
    ):
        """初始化 Milvus 向量库

        Args:
            collection_name: 集合名称（默认 retrieval_kb）
            host: Milvus 主机
            port: Milvus 端口
            embedding_model: Embedding 模型实例
            dimension: 向量维度
        """
        self._collection_name = collection_name
        self._host = host
        self._port = port
        self._embedding_model = embedding_model
        self._dimension = dimension
        self._client = None
        self._collection = None

        # 尝试连接
        try:
            self._connect()
        except Exception as e:
            logger.warning("Failed to connect to Milvus: %s", e)

    def _connect(self):
        """连接到 Milvus"""
        from pymilvus import connections, Collection

        connections.connect(
            alias="default",
            host=self._host,
            port=self._port,
        )

        # 检查集合是否存在，不存在则创建
        from pymilvus import utility
        if utility.has_collection(self._collection_name):
            self._collection = Collection(self._collection_name)
            self._collection.load()
        else:
            self._create_collection()

    def _create_collection(self):
        """创建 Milvus 集合"""
        from pymilvus import Collection, FieldSchema, CollectionSchema, DataType

        # 定义字段：R3.4 增加 tenant_id 分区键字段（pymilvus>=2.4 partition_key）
        fields = [
            FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=64),
            FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=self._dimension),
            FieldSchema(name="metadata", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(
                name="tenant_id", dtype=DataType.VARCHAR, max_length=64,
                partition_key=True, default_value="default",
            ),
        ]

        # 创建集合
        schema = CollectionSchema(fields, description="My Agentix retrieval_kb vector store")
        self._collection = Collection(self._collection_name, schema)

        # 创建索引
        index_params = {
            "metric_type": MILVUS_METRIC_TYPE,
            "index_type": MILVUS_INDEX_TYPE,
            "params": {"nlist": 1024},
        }
        self._collection.create_index("embedding", index_params)
        self._collection.load()

        logger.info("Created Milvus collection: %s", self._collection_name)
    
    def _get_embeddings(self):
        """获取 Embedding 模型"""
        if self._embedding_model is None:
            from agentix.retrieval.infra.embedding.client import create_embeddings
            from agentix.retrieval.config import get_default_config
            self._embedding_model = create_embeddings(get_default_config())
        return self._embedding_model
    
    def _encode_text(self, text: str) -> List[float]:
        """编码文本为向量"""
        embeddings = self._get_embeddings()
        # 假设 embeddings 有 embed_documents 方法
        if hasattr(embeddings, 'embed_documents'):
            return embeddings.embed_documents([text])[0]
        from agentix.retrieval.infra.rag.dense import embed_texts_sync

        vectors = embed_texts_sync([text])
        if not vectors:
            raise ValueError("Embedding 模型未返回向量")
        return vectors[0]
    
    def add_documents(
        self,
        documents: List[VectorDocument],
        ids: Optional[List[str]] = None,
    ) -> List[str]:
        """添加文档到 Milvus"""
        import json
        
        if self._collection is None:
            raise ValueError("Milvus collection not initialized")
        
        # 准备数据
        texts = [doc.text for doc in documents]
        embeddings = [self._encode_text(text) for text in texts]
        metadatas = [json.dumps(doc.metadata or {}, ensure_ascii=False) for doc in documents]
        # R3.4：tenant_id 列（partition_key），默认 default；禁止跨租户复用分区
        tenant_ids = [
            str((doc.metadata or {}).get("tenant_id") or "default") for doc in documents
        ]

        if ids is None:
            ids = [doc.id or f"doc_{i}" for i, doc in enumerate(documents)]

        # 插入数据
        self._collection.insert([ids, texts, embeddings, metadatas, tenant_ids])
        self._collection.flush()
        
        logger.info("Added %d documents to Milvus", len(documents))
        return ids
    
    @staticmethod
    def _tenant_expr(tenant_id: Optional[str]) -> Optional[str]:
        """R3.4：构造租户过滤表达式（拒绝空串与引号注入）。"""
        if not tenant_id:
            return None
        tid = str(tenant_id).strip().replace('"', "").replace("\\", "")
        if not tid:
            return None
        return f'tenant_id == "{tid}"'

    def similarity_search(
        self,
        query: str,
        k: int = 5,
        filter: Optional[dict] = None,
        tenant_id: Optional[str] = None,
        **kwargs,
    ) -> List[tuple[VectorDocument, float]]:
        """Milvus 相似度搜索（R3.4：支持租户过滤，检索范围限定在租户分区内）"""
        import json

        if self._collection is None:
            logger.warning("Milvus collection not initialized")
            return []

        # 编码查询
        query_embedding = self._encode_text(query)

        # 搜索参数（metric 口径见 MILVUS_METRIC_TYPE 注释）；
        # W2：nprobe 配置化——默认 16 = 原硬编码值（行为等价），越界回落下限 1
        try:
            from agentix.config import settings as _app_settings

            nprobe = int(getattr(_app_settings, "MILVUS_NPROBE", 16))
        except (TypeError, ValueError):
            nprobe = 16
        search_params = {
            "metric_type": MILVUS_METRIC_TYPE,
            "params": {"nprobe": max(1, nprobe)},
        }

        # R3.4：租户过滤 + 调用方 filter dict（key=value 相等条件）合并
        expr = self._tenant_expr(tenant_id)
        if filter:
            clauses = [
                f'{key} == "{str(value).replace(chr(34), "").replace(chr(92), "")}"'
                for key, value in filter.items()
            ]
            expr = " and ".join(([expr] if expr else []) + clauses) or None

        # 执行搜索
        results = self._collection.search(
            data=[query_embedding],
            anns_field="embedding",
            param=search_params,
            limit=k,
            expr=expr,
            output_fields=["id", "text", "metadata"],
        )
        
        # 转换结果
        search_results = []
        for hits in results:
            for hit in hits:
                metadata = json.loads(hit.entity.get("metadata", "{}"))
                vec_doc = type('VectorDoc', (), {
                    'text': hit.entity.get("text", ""),
                    'metadata': metadata,
                    'id': hit.id,
                })()
                search_results.append((vec_doc, float(hit.score)))
        
        return search_results
    
    def delete_documents(self, ids: List[str]) -> None:
        """删除文档"""
        if self._collection is None:
            return
        
        # Milvus 删除需要表达式
        expr = f"id in {ids}"
        self._collection.delete(expr)
        logger.info("Deleted %d documents from Milvus", len(ids))
    
    def save(self, path: str) -> None:
        """Milvus 自动持久化，无需手动保存"""
        logger.info("Milvus automatically persists data")
    
    def load(self, path: str) -> None:
        """Milvus 自动加载，无需手动加载"""
        logger.info("Milvus automatically loads data")
    
    def get_retriever(self, k: int = 5, filter: Optional[dict] = None):
        """获取检索器（包装为 LangChain 兼容接口）"""
        from langchain_core.retrievers import BaseRetriever
        from langchain_core.documents import Document as LCDocument
        
        class MilvusRetriever(BaseRetriever):
            """Milvus LangChain 检索器"""
            
            def __init__(self, store: MilvusVectorStore, k: int, filter: Optional[dict]):
                super().__init__()
                self._store = store
                self._k = k
                self._filter = filter
            
            def _get_relevant_documents(self, query: str, **kwargs):
                results = self._store.similarity_search(
                    query,
                    k=self._k,
                    filter=self._filter,
                )
                return [
                    LCDocument(
                        page_content=doc.text,
                        metadata={**doc.metadata, "_retrieval_score": score},
                    )
                    for doc, score in results
                ]
        
        return MilvusRetriever(store=self, k=k, filter=filter)
    
    def count(self) -> int:
        """返回向量库中的文档数量"""
        if self._collection is None:
            return 0
        return self._collection.num_entities


# 注册 Milvus 实现
VectorStoreFactory.register("milvus", MilvusVectorStore)
