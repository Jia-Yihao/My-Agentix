"""
向量库抽象层

提供统一的向量库接口，支持多种后端：
- FAISS：本地向量库，适合开发和小规模数据
- Milvus：分布式向量库，适合大规模生产环境
- Qdrant：高性能向量库，支持过滤和分组

使用方式：
    from agentix.retrieval.infra.vectorstore import VectorStoreManager, VectorStoreFactory
    
    # 使用工厂创建
    store = VectorStoreFactory.create("faiss")
    
    # 使用管理器
    manager = VectorStoreManager(backend="faiss")
    manager.add_documents([...])
"""

from agentix.retrieval.infra.vectorstore.base import (
    VectorDocument,
    VectorStore,
    VectorStoreFactory,
)
from agentix.retrieval.infra.vectorstore.manager import (
    VectorStoreManager,
    create_vector_store,
)
from agentix.retrieval.infra.vectorstore.faiss_impl import FAISSVectorStore

# 导入所有实现以触发注册
import agentix.retrieval.infra.vectorstore.faiss_impl
import agentix.retrieval.infra.vectorstore.milvus_impl
import agentix.retrieval.infra.vectorstore.qdrant_impl

__all__ = [
    # 基础类型
    "VectorDocument",
    "VectorStore",
    "VectorStoreFactory",
    # 管理器
    "VectorStoreManager",
    "create_vector_store",
    # 具体实现
    "FAISSVectorStore",
]
