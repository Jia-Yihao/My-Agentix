"""
FAISS 向量存储管理器（旧版，保留向后兼容）

封装 FAISS 的创建/加载/检索/分块能力。
推荐使用新的 VectorStoreManager（来自 manager.py）。
"""
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import List

from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document

from agentix.retrieval.config import get_default_config
from agentix.retrieval.infra.embedding.client import create_embeddings
from agentix.retrieval.settings import settings

logger = logging.getLogger(__name__)

# 安全配置：是否验证文件完整性（生产环境建议开启）
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


class FAISSVectorStoreManager:
    """FAISS 向量存储管理器（旧版）
    
    注意：此类保留向后兼容。推荐使用新的 VectorStoreManager（来自 manager.py）。
    """

    def __init__(self, config: dict | None = None):
        self.config = config or get_default_config()
        self.vectorstore: FAISS | None = None
        self.embeddings = create_embeddings(self.config)

    def split_documents(self, documents: List[Document]) -> List[Document]:
        """对文档进行智能分块"""
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.config.get("chunk_size", 500),
            chunk_overlap=self.config.get("chunk_overlap", 100),
            length_function=len,
            add_start_index=True,
            separators=["\n\n", "\n", "。", "！", "？", "；", "，", "、", ""]
        )
        texts = text_splitter.split_documents(documents)

        for idx, doc in enumerate(texts):
            doc.metadata["chunk_id"] = idx

        logger.info("文档分割完成，共 %d 个文本块", len(texts))
        return texts

    def create_vector_store(self, documents: List[Document], save_path: str) -> None:
        """创建向量数据库并保存"""
        if not documents:
            raise ValueError("文档列表不能为空")

        logger.info("正在创建向量数据库...")
        # 归一化后 L2 距离只反映方向差异，跨模型/维度更稳定。
        self.vectorstore = FAISS.from_documents(
            documents,
            self.embeddings,
            normalize_L2=settings.VECTOR_NORMALIZE,
        )
        os.makedirs(save_path, exist_ok=True)
        self.vectorstore.save_local(save_path)
        
        # 保存索引元数据（包含文件哈希，用于完整性验证）
        index_file = Path(save_path) / "index.faiss"
        pkl_file = Path(save_path) / "index.pkl"
        metadata = {
            "index_hash": _calculate_file_hash(str(index_file)) if index_file.exists() else None,
            "pkl_hash": _calculate_file_hash(str(pkl_file)) if pkl_file.exists() else None,
            "doc_count": len(documents),
        }
        _save_index_metadata(save_path, metadata)
        
        logger.info("向量数据库已保存到 %s", save_path)

    def load_vector_store(self, load_path: str) -> None:
        """加载已保存的向量数据库"""
        index_file = Path(load_path) / "index.faiss"
        if not index_file.exists():
            raise FileNotFoundError(f"向量数据库不存在: {load_path}")

        # 验证索引文件完整性（可选）
        if not _verify_index_integrity(load_path):
            raise ValueError(
                "索引文件完整性验证失败。可能原因：\n"
                "1. 文件被篡改\n"
                "2. 文件损坏\n"
                "请重新创建索引或设置 VERIFY_INDEX_INTEGRITY=false 跳过验证"
            )

        # 注意：allow_dangerous_deserialization=True 是 FAISS load_local 的必需参数
        # 因为 LangChain 的 FAISS 实现使用 pickle 保存元数据
        # 安全措施：通过文件哈希验证确保文件未被篡改
        self.vectorstore = FAISS.load_local(
            load_path,
            self.embeddings,
            allow_dangerous_deserialization=True,
        )
        logger.info("已加载向量数据库: %s", load_path)

    def get_retriever(self):
        """获取检索器"""
        if not self.vectorstore:
            raise ValueError("向量数据库未初始化，请先创建或加载")
        return self.vectorstore.as_retriever(
            search_type="similarity",
            search_kwargs={"k": self.config.get("search_k", 5)},
        )


# 向后兼容别名
VectorStoreManager = FAISSVectorStoreManager
