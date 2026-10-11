"""
Qdrant 向量库实现

使用 qdrant-client 官方客户端，支持高性能向量库操作。
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from agentix.retrieval.infra.vectorstore.base import VectorDocument, VectorStore, VectorStoreFactory

logger = logging.getLogger(__name__)


class QdrantVectorStore(VectorStore):
    """Qdrant 向量库实现"""
    
    def __init__(
        self,
        collection_name: str = "agentix_retrieval",
        host: str = "localhost",
        port: int = 6333,
        embedding_model: Any = None,
        dimension: int = 1024,
        **kwargs,
    ):
        """初始化 Qdrant 向量库
        
        Args:
            collection_name: 集合名称
            host: Qdrant 主机
            port: Qdrant 端口
            embedding_model: Embedding 模型实例
            dimension: 向量维度
        """
        self._collection_name = collection_name
        self._host = host
        self._port = port
        self._embedding_model = embedding_model
        self._dimension = dimension
        self._client = None
        
        # 尝试连接
        try:
            self._connect()
        except Exception as e:
            logger.warning("Failed to connect to Qdrant: %s", e)
    
    def _connect(self):
        """连接到 Qdrant"""
        from qdrant_client import QdrantClient
        
        self._client = QdrantClient(host=self._host, port=self._port)
        
        # 检查集合是否存在，不存在则创建
        collections = self._client.get_collections().collections
        collection_names = [c.name for c in collections]
        
        if self._collection_name not in collection_names:
            self._create_collection()
        else:
            logger.info("Connected to existing Qdrant collection: %s", self._collection_name)
    
    def _create_collection(self):
        """创建 Qdrant 集合"""
        from qdrant_client.models import VectorParams, Distance
        
        # 创建集合
        self._client.create_collection(
            collection_name=self._collection_name,
            vectors_config=VectorParams(
                size=self._dimension,
                distance=Distance.COSINE,
            ),
        )
        
        # 创建有效载荷索引（用于过滤）
        self._client.create_payload_index(
            collection_name=self._collection_name,
            field_name="metadata",
            field_schema="keyword",
        )
        
        logger.info("Created Qdrant collection: %s", self._collection_name)
    
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
        """添加文档到 Qdrant"""
        import json
        
        if self._client is None:
            raise ValueError("Qdrant client not initialized")
        
        # 准备数据
        points = []
        for i, doc in enumerate(documents):
            doc_id = ids[i] if ids else doc.id or f"doc_{i}"
            embedding = self._encode_text(doc.text)
            payload = {
                "text": doc.text,
                "metadata": json.dumps(doc.metadata or {}, ensure_ascii=False),
            }
            
            from qdrant_client.models import PointStruct
            points.append(PointStruct(
                id=doc_id,
                vector=embedding,
                payload=payload,
            ))
        
        # 插入数据
        self._client.upsert(
            collection_name=self._collection_name,
            points=points,
        )
        
        logger.info("Added %d documents to Qdrant", len(documents))
        return [p.id for p in points]
    
    def similarity_search(
        self,
        query: str,
        k: int = 5,
        filter: Optional[dict] = None,
        **kwargs,
    ) -> List[tuple[VectorDocument, float]]:
        """Qdrant 相似度搜索"""
        import json
        
        if self._client is None:
            logger.warning("Qdrant client not initialized")
            return []
        
        # 编码查询
        query_embedding = self._encode_text(query)
        
        # 构建过滤条件
        query_filter = None
        if filter:
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            conditions = []
            for key, value in filter.items():
                conditions.append(FieldCondition(
                    key=key,
                    match=MatchValue(value=value),
                ))
            query_filter = Filter(must=conditions)
        
        # 执行搜索
        results = self._client.search(
            collection_name=self._collection_name,
            query_vector=query_embedding,
            limit=k,
            query_filter=query_filter,
        )
        
        # 转换结果
        search_results = []
        for result in results:
            metadata = json.loads(result.payload.get("metadata", "{}"))
            vec_doc = type('VectorDoc', (), {
                'text': result.payload.get("text", ""),
                'metadata': metadata,
                'id': result.id,
            })()
            search_results.append((vec_doc, float(result.score)))
        
        return search_results
    
    def delete_documents(self, ids: List[str]) -> None:
        """删除文档"""
        if self._client is None:
            return
        
        self._client.delete(
            collection_name=self._collection_name,
            points_selector=ids,
        )
        logger.info("Deleted %d documents from Qdrant", len(ids))
    
    def save(self, path: str) -> None:
        """Qdrant 自动持久化，无需手动保存"""
        logger.info("Qdrant automatically persists data")
    
    def load(self, path: str) -> None:
        """Qdrant 自动加载，无需手动加载"""
        logger.info("Qdrant automatically loads data")
    
    def get_retriever(self, k: int = 5, filter: Optional[dict] = None):
        """获取检索器（包装为 LangChain 兼容接口）"""
        from langchain_core.retrievers import BaseRetriever
        from langchain_core.documents import Document as LCDocument
        
        class QdrantRetriever(BaseRetriever):
            """Qdrant LangChain 检索器"""
            
            def __init__(self, store: QdrantVectorStore, k: int, filter: Optional[dict]):
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
        
        return QdrantRetriever(store=self, k=k, filter=filter)
    
    def count(self) -> int:
        """返回向量库中的文档数量"""
        if self._client is None:
            return 0
        
        info = self._client.get_collection(self._collection_name)
        return info.points_count or 0


# 注册 Qdrant 实现
VectorStoreFactory.register("qdrant", QdrantVectorStore)
