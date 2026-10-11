"""
Embedding 客户端封装

统一 Embedding 调用接口，支持供应商切换。
"""
from typing import Any

from langchain_openai import OpenAIEmbeddings

from agentix.retrieval.config import get_default_config
from agentix.retrieval.infra.embedding.cache import with_embedding_cache
from agentix.retrieval.settings import settings


class OpenAICompatibleEmbeddings(OpenAIEmbeddings):
    """向 OpenAI 兼容 API 发送原始字符串，避免 tiktoken token ID 数组被拒。"""

    def _get_len_safe_embeddings(
        self,
        texts: list[str],
        *,
        engine: str,
        chunk_size: int | None = None,
        **kwargs: Any,
    ) -> list[list[float]]:
        if not texts:
            return []
        client_kwargs = {**self._invocation_params, **kwargs}
        batch_size = chunk_size or self.chunk_size
        embeddings: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            response = self.client.create(input=batch, **client_kwargs)
            if not isinstance(response, dict):
                response = response.model_dump()
            embeddings.extend(item["embedding"] for item in response["data"])
        return embeddings


def create_embeddings(config: dict | None = None):
    """
    创建 OpenAI 兼容的 Embedding 实例（按配置套上本地结果缓存）

    Args:
        config: 配置字典，默认使用 get_default_config()
    """
    cfg = config or get_default_config()
    model = cfg.get("embedding_model", "text-embedding-3-small")
    embeddings = OpenAICompatibleEmbeddings(
        model=model,
        chunk_size=cfg.get("embedding_chunk_size", 200),
        timeout=cfg.get("embedding_timeout", settings.EMBEDDING_TIMEOUT_SECONDS),
        api_key=cfg.get("embedding_api_key"),
        base_url=cfg.get("embedding_base_url"),
    )
    # 单一收口：所有向量后端（faiss / milvus / qdrant）与检索路径都经由此处取 embeddings
    return with_embedding_cache(embeddings, model=model)
