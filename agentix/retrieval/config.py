"""
LLM / Embedding 统一配置
"""
from typing import Dict, Any

from agentix.retrieval.settings import settings


def get_default_config() -> Dict[str, Any]:
    """获取默认配置（LLM + Embedding + 分块参数）"""
    llm_api_key = settings.LLM_API_KEY
    llm_base_url = settings.LLM_BASE_URL
    llm_model = settings.LLM_MODEL

    embedding_api_key = settings.EMBEDDING_API_KEY or llm_api_key
    embedding_base_url = settings.EMBEDDING_BASE_URL or llm_base_url

    return {
        "chunk_size": settings.KB_CHUNK_SIZE,
        "chunk_overlap": settings.KB_CHUNK_OVERLAP,
        "search_k": settings.KB_TOP_K,
        "memory_window": 10,
        "temperature": settings.LLM_TEMPERATURE,
        # LLM
        "model": settings.CHAT_MODEL or llm_model,
        "llm_api_key": llm_api_key,
        "llm_base_url": llm_base_url,
        # Embedding
        "embedding_model": settings.EMBEDDING_MODEL,
        "embedding_api_key": embedding_api_key,
        "embedding_base_url": embedding_base_url,
        "embedding_chunk_size": 32,
        "embedding_timeout": settings.EMBEDDING_TIMEOUT_SECONDS,
        "llm_timeout": settings.LLM_TIMEOUT_SECONDS,
        "thinking_timeout": 90,
        "thinking_max_tokens": 2048,
        "llm_max_retries": 3,
        "vector_store_path": "./faiss_index",
        "max_tokens": 1000,
    }


def merge_config(user_config: Dict[str, Any] = None) -> Dict[str, Any]:
    default_config = get_default_config()
    if user_config is None:
        return default_config
    return {**default_config, **user_config}
