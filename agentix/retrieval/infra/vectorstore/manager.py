"""
向量库管理器

提供统一的向量库管理接口，支持多种后端的动态切换。
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional

from agentix.retrieval.infra.vectorstore.base import VectorDocument, VectorStore, VectorStoreFactory

# 导入所有实现以触发注册

logger = logging.getLogger(__name__)


class VectorStoreManager:
    """向量库管理器"""
    
    def __init__(
        self,
        backend: Optional[str] = None,
        config: Optional[dict] = None,
    ):
        """初始化向量库管理器
        
        Args:
            backend: 向量库后端名称（faiss/milvus/qdrant）
            config: 向量库配置
        """
        self._backend = backend or os.getenv("VECTOR_STORE_BACKEND", "faiss")
        self._config = config or {}
        self._store: Optional[VectorStore] = None
        
        # 创建向量库实例
        self._create_store()
    
    def _create_store(self):
        """创建向量库实例"""
        try:
            self._store = VectorStoreFactory.create(
                self._backend,
                **self._config,
            )
            logger.info("Created vector store: %s", self._backend)
        except Exception as e:
            logger.error("Failed to create vector store %s: %s", self._backend, e)
            # 回退到 FAISS
            if self._backend != "faiss":
                logger.info("Falling back to FAISS")
                self._store = VectorStoreFactory.create("faiss", **self._config)
    
    @property
    def store(self) -> VectorStore:
        """获取向量库实例"""
        if self._store is None:
            raise ValueError("Vector store not initialized")
        return self._store
    
    def add_documents(
        self,
        documents: List[VectorDocument],
        ids: Optional[List[str]] = None,
    ) -> List[str]:
        """添加文档"""
        return self.store.add_documents(documents, ids)
    
    def similarity_search(
        self,
        query: str,
        k: int = 5,
        filter: Optional[dict] = None,
        **kwargs,
    ) -> List[tuple[VectorDocument, float]]:
        """相似度搜索"""
        return self.store.similarity_search(query, k, filter, **kwargs)
    
    def delete_documents(self, ids: List[str]) -> None:
        """删除文档"""
        self.store.delete_documents(ids)
    
    def save(self, path: str) -> None:
        """保存向量库"""
        self.store.save(path)
    
    def load(self, path: str) -> None:
        """加载向量库"""
        self.store.load(path)
    
    def get_retriever(self, k: int = 5, filter: Optional[dict] = None):
        """获取检索器"""
        return self.store.get_retriever(k, filter)
    
    def count(self) -> int:
        """返回文档数量"""
        return self.store.count()
    
    @classmethod
    def get_available_backends(cls) -> List[str]:
        """获取可用的向量库后端列表"""
        return VectorStoreFactory.get_available()
    
    @classmethod
    def from_config(cls, config: dict) -> VectorStoreManager:
        """从配置创建管理器
        
        Args:
            config: 配置字典，包含 backend 和其他参数
            
        Returns:
            VectorStoreManager 实例
        """
        backend = config.pop("backend", None)
        return cls(backend=backend, config=config)


# 便捷函数
def create_vector_store(
    backend: Optional[str] = None,
    **kwargs,
) -> VectorStoreManager:
    """创建向量库管理器
    
    Args:
        backend: 向量库后端名称
        **kwargs: 其他配置参数
        
    Returns:
        VectorStoreManager 实例
    """
    return VectorStoreManager(backend=backend, config=kwargs)
