"""遗留修复：AppError → 标准错误响应的全局转换器。

背景：此前检索域路由抛出的 AppError（UNAUTHORIZED / ANSWER_NOT_FOUND 等）
没有任何 exception_handler，Starlette ServerErrorMiddleware 一律收敛为 500
——authz.py / 各路由设置的 status_code 形同虚设。

本文件通过**真实 application**（module 级 TestClient、不进 lifespan，
同 test_p37 口径）验证转换契约：
- 未带 token 访问需要认证的检索域端点 → 401 + UNAUTHORIZED（此前为 500）
- 非 AppError 异常仍由 global_exception_handler 收敛为 500（契约不变）
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.slow

CLIENT_TIMEOUT = 120


@pytest.fixture(scope="module")
def client():
    from starlette.testclient import TestClient

    import agentix.main as main_module

    return TestClient(main_module.application, raise_server_exceptions=False)


class TestAppErrorGlobalHandler:
    def test_unauthorized_maps_to_401_with_standard_body(self, client):
        """AppError(UNAUTHORIZED) 应返回 401 + 标准错误体（此前 500）。"""
        resp = client.get("/api/v1/retrieval/knowledge-bases", timeout=CLIENT_TIMEOUT)
        assert resp.status_code == 401, f"期望 401，实际 {resp.status_code}: {resp.text[:200]}"
        body = resp.json()
        assert body["error_code"] == "UNAUTHORIZED"
        assert body["user_hint"] == "请先登录"
        assert "Traceback" not in resp.text

    def test_non_app_error_still_collapses_to_500(self, client):
        """非 AppError 异常仍走 global_exception_handler → 500（契约不变）。"""
        resp = client.get("/api/v1/ops/dashboard", timeout=CLIENT_TIMEOUT)
        assert resp.status_code == 500
        body = resp.json()
        assert "error" in body

    def test_error_code_status_mapping_covers_not_found_codes(self):
        """404 类错误码必须在 ERROR_CODE_TO_STATUS 中登记为 404。"""
        from agentix.retrieval.api.response import ERROR_CODE_TO_STATUS, ErrorCode

        for code in (
            ErrorCode.ANSWER_NOT_FOUND,
            ErrorCode.DOCUMENT_NOT_FOUND,
            ErrorCode.CONVERSATION_NOT_FOUND,
            ErrorCode.KB_NOT_FOUND,
        ):
            assert ERROR_CODE_TO_STATUS[code] == 404
