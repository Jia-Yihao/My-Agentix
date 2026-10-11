"""R1-4：上传接口单独限流配额测试。

依据：《技术变更与架构演进指南 v2.0》§2.1.5②——上传接口单独配额，
限流在进入 LLM 前生效（429 不消耗 token）。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentix.interfaces.http.rate_limit_middleware import GlobalRateLimitMiddleware


def _build_app(enabled: bool = True) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        GlobalRateLimitMiddleware,
        rpm=600,
        burst=100,  # 普通路径配额很宽
        enabled=enabled,
        upload_rpm=20,
        upload_burst=2,  # 上传桶容量 2
    )

    @app.post("/api/v1/upload/file")
    async def upload_file():
        return {"ok": True}

    @app.post("/api/v1/retrieval/documents")
    async def upload_doc():
        return {"ok": True}

    @app.get("/hello")
    async def hello():
        return {"ok": True}

    return app


def test_upload_path_uses_stricter_bucket():
    """上传桶容量 2：第 3 次上传 429，而普通路径不受影响。"""
    app = _build_app()
    with TestClient(app) as client:
        assert client.post("/api/v1/upload/file").status_code == 200
        assert client.post("/api/v1/upload/file").status_code == 200
        resp = client.post("/api/v1/upload/file")
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers

        # 普通路径仍正常（独立桶）
        assert client.get("/hello").status_code == 200


def test_retrieval_documents_upload_also_throttled():
    """检索域文档上传路径同口径受限。"""
    app = _build_app()
    with TestClient(app) as client:
        assert client.post("/api/v1/retrieval/documents").status_code == 200
        assert client.post("/api/v1/retrieval/documents").status_code == 200
        assert client.post("/api/v1/retrieval/documents").status_code == 429


def test_upload_and_normal_buckets_independent():
    """普通路径打满不影响上传桶，反之亦然。"""
    app = _build_app()
    with TestClient(app) as client:
        # 上传桶打满
        client.post("/api/v1/upload/file")
        client.post("/api/v1/upload/file")
        assert client.post("/api/v1/upload/file").status_code == 429
        # 普通路径配额独立、仍可用
        assert client.get("/hello").status_code == 200


def test_upload_quota_disabled_with_middleware():
    """enabled=False 时上传路径也放行（灰度开关同口径）。"""
    app = _build_app(enabled=False)
    with TestClient(app) as client:
        for _ in range(5):
            assert client.post("/api/v1/upload/file").status_code == 200
