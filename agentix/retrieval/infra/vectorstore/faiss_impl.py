"""
FAISS 向量库实现

使用 langchain-community 的 FAISS 封装，支持本地向量库操作。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, List, Optional

from agentix.retrieval.infra.vectorstore.base import VectorDocument, VectorStore, VectorStoreFactory
from agentix.retrieval.settings import settings

logger = logging.getLogger(__name__)

# 安全配置：是否验证文件完整性（生产环境建议开启）——默认值统一在 settings.py
VERIFY_INDEX_INTEGRITY = settings.VERIFY_INDEX_INTEGRITY


def _calculate_file_hash(file_path: str) -> str:
    """计算文件的 SHA256 哈希值"""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def _save_index_metadata(index_path: str, metadata: dict) -> None:
    """保存索引元数据（包含文件哈希）"""
    metadata_path = Path(index_path) / "index_metadata.json"
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)


def _load_index_metadata(index_path: str) -> dict | None:
    """加载索引元数据"""
    metadata_path = Path(index_path) / "index_metadata.json"
    if not metadata_path.exists():
        return None
    try:
        with open(metadata_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("加载索引元数据失败: %s", e)
        return None


def _verify_index_integrity(index_path: str) -> bool:
    """验证索引文件完整性"""
    if not VERIFY_INDEX_INTEGRITY:
        return True
    
    metadata = _load_index_metadata(index_path)
    if not metadata:
        logger.warning("索引元数据不存在，跳过完整性验证")
        return True
    
    index_file = Path(index_path) / "index.faiss"
    pkl_file = Path(index_path) / "index.pkl"
    
    if not index_file.exists() or not pkl_file.exists():
        logger.error("索引文件不完整")
        return False
    
    # 验证文件哈希
    current_hash = _calculate_file_hash(str(index_file))
    expected_hash = metadata.get("index_hash")
    if expected_hash and current_hash != expected_hash:
        logger.error("index.faiss 文件哈希不匹配，可能被篡改")
        return False
    
    current_hash = _calculate_file_hash(str(pkl_file))
    expected_hash = metadata.get("pkl_hash")
    if expected_hash and current_hash != expected_hash:
        logger.error("index.pkl 文件哈希不匹配，可能被篡改")
        return False
    
    logger.info("索引文件完整性验证通过")
    return True


class FAISSVectorStore(VectorStore):
    """FAISS 向量库实现"""
    
    def __init__(
        self,
        embedding_model: Any = None,
        index_path: Optional[str] = None,
        **kwargs,
    ):
        """初始化 FAISS 向量库
        
        Args:
            embedding_model: Embedding 模型实例
            index_path: 索引路径（可选）
        """
        self._embedding_model = embedding_model
        self._normalize = kwargs.get("normalize_L2", True)
        self._index_path = index_path
        self._vectorstore = None
        self._retriever = None
        
        # 如果提供了索引路径，尝试加载
        if index_path and os.path.isfile(os.path.join(index_path, "index.faiss")):
            self.load(index_path)
    
    def _get_embeddings(self):
        """获取 Embedding 模型"""
        if self._embedding_model is None:
            from agentix.retrieval.infra.embedding.client import create_embeddings
            from agentix.retrieval.config import get_default_config
            self._embedding_model = create_embeddings(get_default_config())
        return self._embedding_model
    
    def add_documents(
        self,
        documents: List[VectorDocument],
        ids: Optional[List[str]] = None,
    ) -> List[str]:
        """添加文档到 FAISS 向量库"""
        from langchain_core.documents import Document as LCDocument
        
        # 转换为 LangChain Document
        lc_docs = []
        for doc in documents:
            lc_doc = LCDocument(
                page_content=doc.text,
                metadata=doc.metadata or {},
            )
            if hasattr(doc, 'id') and doc.id:
                lc_doc.metadata['id'] = doc.id
            lc_docs.append(lc_doc)
        
        # 创建或更新向量库
        if self._vectorstore is None:
            from langchain_community.vectorstores import FAISS
            self._vectorstore = FAISS.from_documents(
                lc_docs,
                self._get_embeddings(),
                normalize_L2=self._normalize,
            )
        else:
            self._vectorstore.add_documents(lc_docs)
        
        # 返回添加的文档ID
        if ids:
            return ids
        return [doc.metadata.get('id', str(i)) for i, doc in enumerate(documents)]
    
    def similarity_search(
        self,
        query: str,
        k: int = 5,
        filter: Optional[dict] = None,
        **kwargs,
    ) -> List[tuple[VectorDocument, float]]:
        """FAISS 相似度搜索"""
        if self._vectorstore is None:
            logger.warning("Vector store not initialized, returning empty results")
            return []
        
        # 执行搜索
        pairs = self._vectorstore.similarity_search_with_score(
            query,
            k=k,
            filter=filter,
            **kwargs,
        )
        
        # 转换为 VectorDocument
        results = []
        for doc, score in pairs:
            vec_doc = type('VectorDoc', (), {
                'text': doc.page_content,
                'metadata': doc.metadata or {},
                'id': doc.metadata.get('id'),
            })()
            results.append((vec_doc, float(score)))
        
        return results
    
    def delete_documents(self, ids: List[str]) -> None:
        """删除文档（FAISS 不直接支持删除，需要重建索引）"""
        logger.warning("FAISS does not support direct deletion. Consider rebuilding the index.")
    
    def save(self, path: str) -> None:
        """保存 FAISS 索引到磁盘"""
        if self._vectorstore is None:
            raise ValueError("Vector store not initialized")
        
        os.makedirs(path, exist_ok=True)
        self._vectorstore.save_local(path)
        
        # 保存索引元数据（包含文件哈希，用于完整性验证）
        index_file = Path(path) / "index.faiss"
        pkl_file = Path(path) / "index.pkl"
        metadata = {
            "index_hash": _calculate_file_hash(str(index_file)) if index_file.exists() else None,
            "pkl_hash": _calculate_file_hash(str(pkl_file)) if pkl_file.exists() else None,
            "doc_count": self.count(),
        }
        _save_index_metadata(path, metadata)
        
        self._index_path = path
        logger.info("FAISS index saved to %s", path)
    
    def load(self, path: str) -> None:
        """从磁盘加载 FAISS 索引"""
        from langchain_community.vectorstores import FAISS
        
        if not os.path.exists(path):
            raise FileNotFoundError(f"Index path not found: {path}")
        
        # 验证索引文件完整性（可选）
        if not _verify_index_integrity(path):
            raise ValueError(
                "索引文件完整性验证失败。可能原因：\n"
                "1. 文件被篡改\n"
                "2. 文件损坏\n"
                "请重新创建索引或设置 VERIFY_INDEX_INTEGRITY=false 跳过验证"
            )
        
        # 安全加载（验证文件完整性）
        self._vectorstore = FAISS.load_local(
            path,
            self._get_embeddings(),
            allow_dangerous_deserialization=True,
        )
        self._index_path = path
        logger.info("FAISS index loaded from %s", path)
    
    def get_retriever(self, k: int = 5, filter: Optional[dict] = None):
        """获取 LangChain 兼容的检索器"""
        if self._vectorstore is None:
            raise ValueError("Vector store not initialized")
        
        return self._vectorstore.as_retriever(
            search_type="similarity",
            search_kwargs={"k": k, "filter": filter} if filter else {"k": k},
        )
    
    def count(self) -> int:
        """返回向量库中的文档数量"""
        if self._vectorstore is None:
            return 0
        
        index = getattr(self._vectorstore, "index", None)
        if index is not None:
            return getattr(index, "ntotal", 0)
        return 0


# 注册 FAISS 实现
VectorStoreFactory.register("faiss", FAISSVectorStore)
