"""RetrievalPort 协议与 Adapter 单元测试（R1.13）。"""

from __future__ import annotations

import pytest

from agentix.domain.ports.retrieval import (
    IndexStatus,
    RetrievalChunk,
    RetrievalPort,
    RetrievalResult,
)
from agentix.retrieval.usecases.retrieval_port_adapter import (
    RetrievalPortAdapter,
    create_retrieval_port,
)


class TestRetrievalPortProtocol:
    """RetrievalPort Protocol 结构与契约。"""

    def test_retrieval_result_typedict_fields(self):
        result: RetrievalResult = {
            "status": "ok",
            "chunks": [],
            "error": None,
        }
        assert result["status"] in ("ok", "empty", "degraded")

    def test_retrieval_chunk_typedict_fields(self):
        chunk: RetrievalChunk = {
            "chunk_id": "c1",
            "content": "text",
            "score": 0.9,
            "citation": {
                "kb_id": "kb1",
                "doc_id": "doc1",
                "doc_title": "Title",
                "page": 1,
                "tenant_id": "t1",
            },
        }
        assert chunk["chunk_id"] == "c1"
        assert chunk["citation"]["doc_id"] == "doc1"

    def test_index_status_typedict_fields(self):
        status: IndexStatus = {
            "kb_id": "kb1",
            "status": "ready",
            "progress": None,
            "error_message": None,
        }
        assert status["status"] in ("ready", "indexing", "failed", "unknown")


class TestRetrievalPortAdapter:
    """RetrievalPortAdapter 实例化与 Protocol 满足。"""

    def test_adapter_satisfies_protocol(self):
        adapter = RetrievalPortAdapter()
        assert isinstance(adapter, RetrievalPort)

    def test_factory_creates_adapter(self):
        adapter = create_retrieval_port()
        assert isinstance(adapter, RetrievalPort)
        assert isinstance(adapter, RetrievalPortAdapter)

    def test_adapter_has_required_methods(self):
        adapter = RetrievalPortAdapter()
        assert hasattr(adapter, "search")
        assert hasattr(adapter, "delete_by_doc")
        assert hasattr(adapter, "index_status")
        import asyncio
        assert asyncio.iscoroutinefunction(adapter.search)
        assert asyncio.iscoroutinefunction(adapter.delete_by_doc)
        assert asyncio.iscoroutinefunction(adapter.index_status)


class TestRetrievalPortAdapterSearch:
    """RetrievalPortAdapter.search 边界行为。"""

    @pytest.mark.asyncio
    async def test_empty_query_returns_empty(self):
        adapter = RetrievalPortAdapter()
        result = await adapter.search(
            query="",
            kb_id="test-kb",
            tenant_id="test-tenant",
            top_k=5,
        )
        assert result["status"] == "empty"
        assert result["chunks"] == []
        assert result["error"] is None

    @pytest.mark.asyncio
    async def test_whitespace_query_returns_empty(self):
        adapter = RetrievalPortAdapter()
        result = await adapter.search(
            query="   ",
            kb_id="test-kb",
            tenant_id="test-tenant",
            top_k=5,
        )
        assert result["status"] == "empty"
        assert result["chunks"] == []
