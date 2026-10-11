"""v1 逐端点鉴权（接口审查批次 2026-09-27；审计批次 A2 修订 2026-09-28）。

钉死三层契约：

1. 用户面（chat / sessions / upload **含读操作**）：``V1_REQUIRE_JWT`` 默认关
   = 匿名直通（strangler 纪律，flag-off 行为不变）；开启后无/坏 Bearer → 401，
   有效 JWT → 放行到业务层。
   - 审计批次 A2（2026-09-28）：原「upload GET 文件读取豁免（渲染面取不到
     Authorization 头）」是写读不对称的 IDOR 面，已改为与写一致——读也要凭证
     + owner sidecar 比对（归属放行/拒绝见 `v1/upload.py::_owner_allows`）。
2. 机器面（events / ops）：端点级 ``verify_api_key`` —— ``REQUIRE_API_KEY``
   关 = 直通；开 = 无/错 x-api-key → 401，对 = 放行（不依赖全局中间件，
   为轻量装配面兜底）。
3. 两开关正交：V1_REQUIRE_JWT 不影响机器面，REQUIRE_API_KEY 不影响用户面。

用例全部走轻量 app（无中间件）——这正是端点级依赖存在的意义。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentix.config import settings
from agentix.security.jwt import create_access_token


def _light_app() -> FastAPI:
    """无中间件的轻量装配——中间件管不到的面正是端点级依赖要兜的底。"""
    from agentix.interfaces.http import ops_router
    from agentix.interfaces.http.v1 import api_router

    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    app.include_router(ops_router.router, prefix="/api/v1")
    return app


def _client() -> TestClient:
    return TestClient(_light_app(), raise_server_exceptions=False)


class TestUserPlaneJwt:
    def test_flag_off_is_open_by_default(self):
        """默认关：既有匿名行为零变化。"""
        resp = _client().get("/api/v1/sessions/", params={"user_id": "u-open"})
        assert resp.status_code != 401

    def test_flag_on_rejects_missing_token(self, monkeypatch):
        monkeypatch.setattr(settings, "V1_REQUIRE_JWT", True)
        resp = _client().get("/api/v1/sessions/", params={"user_id": "u-401"})
        assert resp.status_code == 401
        assert "detail" in resp.json()

    def test_flag_on_rejects_garbage_token(self, monkeypatch):
        monkeypatch.setattr(settings, "V1_REQUIRE_JWT", True)
        resp = _client().get(
            "/api/v1/sessions/",
            params={"user_id": "u-401"},
            headers={"Authorization": "Bearer not-a-jwt"},
        )
        assert resp.status_code == 401

    def test_flag_on_accepts_valid_jwt(self, monkeypatch):
        """有效 JWT → 通过鉴权进入业务层（离线 DB 故障只该 500，绝不能 401）。"""
        monkeypatch.setattr(settings, "V1_REQUIRE_JWT", True)
        token = create_access_token(user_id="u-ok")
        resp = _client().get(
            "/api/v1/sessions/",
            params={"user_id": "u-ok"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code != 401

    def test_chat_post_requires_token_when_flag_on(self, monkeypatch):
        monkeypatch.setattr(settings, "V1_REQUIRE_JWT", True)
        resp = _client().post("/api/v1/chat", json={"message": "hi"})
        assert resp.status_code == 401

    def test_upload_mutations_and_reads_both_guarded(self, monkeypatch):
        """写读同口径（审计批次 A2）：无凭证时写=读=401。"""
        monkeypatch.setattr(settings, "V1_REQUIRE_JWT", True)
        client = _client()
        # 写操作（DELETE）必须挡
        assert client.delete("/api/v1/upload/any-file").status_code == 401
        # 读操作同样挡（A2 取消原「渲染面豁免」——写读不对称是 IDOR 面）
        assert client.get("/api/v1/upload/files/whatever.txt").status_code == 401

    def test_upload_read_with_valid_jwt_reaches_owner_layer(self, monkeypatch):
        """有效 JWT → 过鉴权进归属层：文件不存在 = 404（绝非 401）。"""
        monkeypatch.setattr(settings, "V1_REQUIRE_JWT", True)
        token = create_access_token(user_id="u-read")
        resp = _client().get(
            "/api/v1/upload/files/whatever.txt",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404


class TestMachinePlaneApiKey:
    @pytest.fixture(autouse=True)
    def _api_key_on(self, monkeypatch):
        monkeypatch.setattr(settings, "REQUIRE_API_KEY", True)
        monkeypatch.setattr(settings, "API_KEY", "s3cret-key")

    def test_events_rejects_missing_key(self):
        resp = _client().post("/api/v1/events", json={"event_type": "ping"})
        assert resp.status_code == 401

    def test_events_rejects_wrong_key(self):
        resp = _client().post(
            "/api/v1/events",
            json={"event_type": "ping"},
            headers={"x-api-key": "wrong"},
        )
        assert resp.status_code == 401

    def test_events_accepts_correct_key(self):
        resp = _client().post(
            "/api/v1/events",
            json={"event_type": "ping", "payload": {"n": 1}},
            headers={"x-api-key": "s3cret-key"},
        )
        assert resp.status_code == 202

    def test_ops_requires_key(self):
        client = _client()
        assert client.get("/api/v1/ops/dashboard").status_code == 401
        resp = client.get(
            "/api/v1/ops/dashboard", headers={"x-api-key": "s3cret-key"}
        )
        assert resp.status_code != 401  # 离线可 200 或 500，绝不能 401

    def test_machine_flag_does_not_leak_to_user_plane(self, monkeypatch):
        """REQUIRE_API_KEY 开 ≠ 用户面要求 JWT（两把闸门正交）。"""
        resp = _client().get("/api/v1/sessions/", params={"user_id": "u-x"})
        assert resp.status_code != 401

    def test_api_key_off_means_open(self, monkeypatch):
        monkeypatch.setattr(settings, "REQUIRE_API_KEY", False)
        resp = _client().post("/api/v1/events", json={"event_type": "ping"})
        assert resp.status_code == 202
