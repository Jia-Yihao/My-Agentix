"""与向量后端共享的同步 embedding 适配器。"""

from __future__ import annotations

import os
from typing import Any

import httpx

from agentix.retrieval.settings import settings


def embed_texts_sync(
    texts: list[str],
    model: str | None = None,
    base_url: str | None = None,
) -> list[list[float]]:
    """调用 OpenAI-compatible embedding 服务，供同步向量后端使用。"""
    endpoint = (base_url or os.getenv("EMBEDDING_BASE_URL", "http://127.0.0.1:1234/v1")).rstrip("/") + "/embeddings"
    payload = {"model": model or os.getenv("EMBEDDING_MODEL", "text-embedding-bge-m3"), "input": texts}
    response = httpx.post(
        endpoint,
        json=payload,
        timeout=settings.EMBEDDING_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data: Any = response.json().get("data", [])
    return [item["embedding"] for item in data]
