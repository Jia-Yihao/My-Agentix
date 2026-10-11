"""
向量库抽象层

提供统一的向量库接口，支持多种后端：
- FAISS：本地向量库，适合开发和小规模数据
- Milvus：分布式向量库，适合大规模生产环境
- Qdrant：高性能向量库，支持过滤和分组
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, List, Optional, Protocol

logger = logging.getLogger(__name__)


class VectorDocument(Protocol):
    """向量文档协议"""
    text: str
    metadata: dict[str, Any]
    id: Optional[str]


@dataclass(frozen=True)
class StoredVectorDocument:
    """供索引编排层传入向量库的最小 DTO。"""

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    id: Optional[str] = None


class VectorStore(ABC):
    """向量库抽象基类"""
    
    @abstractmethod
    def add_documents(
        self,
        documents: List[VectorDocument],
        ids: Optional[List[str]] = None,
    ) -> List[str]:
        """添加文档到向量库
        
        Args:
            documents: 文档列表
            ids: 文档ID列表（可选）
            
        Returns:
            添加的文档ID列表
        """
        ...
    
    @abstractmethod
    def similarity_search(
        self,
        query: str,
        k: int = 5,
        filter: Optional[dict] = None,
        **kwargs,
    ) -> List[tuple[VectorDocument, float]]:
        """相似度搜索
        
        Args:
            query: 查询文本
            k: 返回结果数量
            filter: 过滤条件
            
        Returns:
            (文档, 分数) 列表，按分数降序排列
        """
        ...
    
    @abstractmethod
    def delete_documents(self, ids: List[str]) -> None:
        """删除文档
        
        Args:
            ids: 要删除的文档ID列表
        """
        ...
    
    @abstractmethod
    def save(self, path: str) -> None:
        """保存向量库到磁盘
        
        Args:
            path: 保存路径
        """
        ...
    
    @abstractmethod
    def load(self, path: str) -> None:
        """从磁盘加载向量库
        
        Args:
            path: 加载路径
        """
        ...
    
    @abstractmethod
    def get_retriever(self, k: int = 5, filter: Optional[dict] = None):
        """获取检索器（兼容 LangChain）
        
        Args:
            k: 返回结果数量
            filter: 过滤条件
            
        Returns:
            LangChain 兼容的检索器
        """
        ...
    
    @abstractmethod
    def count(self) -> int:
        """返回向量库中的文档数量"""
        ...


class VectorStoreFactory:
    """向量库工厂类"""
    
    _stores: dict[str, type[VectorStore]] = {}
    
    @classmethod
    def register(cls, name: str, store_class: type[VectorStore]) -> None:
        """注册向量库实现
        
        Args:
            name: 向量库名称
            store_class: 向量库实现类
        """
        cls._stores[name] = store_class
        logger.info("Registered vector store: %s", name)
    
    @classmethod
    def create(cls, name: str, **kwargs) -> VectorStore:
        """创建向量库实例
        
        Args:
            name: 向量库名称
            **kwargs: 传递给向量库构造函数的参数
            
        Returns:
            向量库实例
        """
        if name not in cls._stores:
            available = ", ".join(cls._stores.keys())
            raise ValueError(
                f"Unknown vector store: {name}. Available: {available}"
            )
        return cls._stores[name](**kwargs)
    
    @classmethod
    def get_available(cls) -> List[str]:
        """获取可用的向量库列表"""
        return list(cls._stores.keys())
