"""审计批次 A1/A2 归属校验（安全审计 2026-09-28 · Phase28-31 前置批次 A）。

钉死契约：
1. **A1 会话/历史归属**：调用方携带身份声明（``X-Device-ID`` 头，或 JWT regime
   下的 Bearer sub）时，跨用户读写他人会话一律 404（防存在性枚举）；同用户 200；
   无声明 = 匿名轻量域，**行为不变**（strangler 纪律）。
2. **A1 身份覆盖**：声明身份与 body/query 的 ``user_id`` 冲突时——create 强制
   归属身份；list/count 跨用户 404；PATCH 不得改写他人 user_id。
3. **A1 写路径**：chat 流式 / 历史 / 清空不得写入他人会话（404 先于业务链路）。
4. **A1 JWT regime**：``V1_REQUIRE_JWT`` 开启时身份 = JWT sub（服务端签发，
   ``X-Device-ID`` 头不可伪造身份）；无 token 401。
5. **A1 匿名桶**：前身份期 ``default_user`` 会话对任何声明身份可见（迁移不破坏）。
6. **A2 上传归属**：上传落 owner sidecar（``UPLOAD_DIR/.owners/<name>.owner``），
   读/删比对；无 sidecar = 历史/匿名文件放行；flag 开时读也要凭证。

全部离线：会话 CRUD 走内存替身、chat 走脚本化 stub、上传走临时目录——
不碰网络、不依赖 DB 表存在（测试线程 SQLite 内存库无表，见 test_p37 口径）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import agentix.interfaces.http.v1.chat as chat_module
import agentix.interfaces.http.v1.sessions as sessions_module
import agentix.interfaces.http.v1.upload as upload_module
from agentix.config import settings
from agentix.security.jwt import create_access_token


# ---------------------------------------------------------------------------
# 替身：内存会话 CRUD / 消息 CRUD / 脚本化 ChatService（零 DB、零 LLM）
# ---------------------------------------------------------------------------


class _SessionRow:
    def __init__(self, id: str, user_id, title: str = "t", is_active: bool = True):
        self.id = id
        self.user_id = user_id
        self.title = title
        self.is_active = is_active
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = self.created_at


class _MessageRow:
    _next_id = 10_000

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        _MessageRow._next_id += 1
        self.id = _MessageRow._next_id
        self.created_at = datetime.now(timezone.utc)


class _FakeSessionCRUD:
    """sessions.py 的 ``chat_session`` 替身（端点签名逐一对齐）。"""

    def __init__(self):
        self.rows: dict[str, _SessionRow] = {}

    def seed(self, sid: str, user_id, title: str = "t") -> _SessionRow:
        row = _SessionRow(id=sid, user_id=user_id, title=title)
        self.rows[sid] = row
        return row

    def get(self, db, id: str):
        return self.rows.get(id)

    def get_by_user(
        self, db, *, user_id, skip: int = 0, limit: int = 50, active_only: bool = True
    ):
        rows = [
            r
            for r in self.rows.values()
            if r.user_id == user_id and (r.is_active or not active_only)
        ]
        return rows[skip : skip + limit]

    def create(self, db, *, obj_in):
        row = _SessionRow(
            id=obj_in["id"],
            user_id=obj_in.get("user_id"),
            title=obj_in.get("title", "t"),
        )
        self.rows[row.id] = row
        return row

    def update(self, db, *, db_obj, obj_in):
        for k, v in obj_in.model_dump(exclude_unset=True).items():
            setattr(db_obj, k, v)
        db_obj.updated_at = datetime.now(timezone.utc)
        return db_obj

    def soft_delete(self, db, *, session_id: str):
        self.rows[session_id].is_active = False
        self.rows[session_id].updated_at = datetime.now(timezone.utc)

    def update_activity(self, db, *, session_id: str):
        if session_id in self.rows:
            self.rows[session_id].updated_at = datetime.now(timezone.utc)

    def count_by_user(self, db, *, user_id, active_only: bool = True) -> int:
        return len(self.get_by_user(db, user_id=user_id, active_only=active_only, limit=10_000))


class _FakeMessageCRUD:
    def create(self, db, *, obj_in):
        return _MessageRow(**dict(obj_in))


class _ScriptedChatService:
    """chat.py 的 ``_chat_service`` 替身：owners = 会话归属脚本。"""

    def __init__(self, owners: dict[str, str] | None = None):
        self.owners = owners if owners is not None else {}
        self.save_calls: list[dict] = []

    def session_exists(self, db, session_id):
        return session_id in self.owners

    def get_session_owner(self, db, session_id):
        return self.owners.get(session_id)

    def get_or_create_session(self, db, session_id, user_id):
        return session_id or "p48-new-session"

    def list_recent_messages(self, db, *, session_id, skip=0, limit=100):
        return []

    def clear_session_messages(self, db, *, session_id):
        return 0

    async def save_message(self, db, session_id, message, is_user, route=None, metadata=None):
        self.save_calls.append({"session_id": session_id, "message": message, "is_user": is_user})
        return f"m-{len(self.save_calls)}"

    async def stream_events(self, message, session_id, image_path=None, file_path=None,
                            ingest_incremental=None):
        yield {"type": "done", "metadata": {"sources": []}, "session_id": session_id}


@pytest.fixture()
def api(monkeypatch, tmp_path):
    """离线装配：api_router 全量 + 三处替身 + 临时上传目录。"""
    fake_sessions = _FakeSessionCRUD()
    monkeypatch.setattr(sessions_module, "chat_session", fake_sessions)
    monkeypatch.setattr(sessions_module, "chat_message", _FakeMessageCRUD())
    stub_chat = _ScriptedChatService()
    monkeypatch.setattr(chat_module, "_chat_service", stub_chat)
    monkeypatch.setattr(upload_module, "UPLOAD_DIR", tmp_path)

    app = FastAPI()
    from agentix.interfaces.http.v1 import api_router

    app.include_router(api_router, prefix="/api/v1")
    client = TestClient(app, raise_server_exceptions=False)
    return SimpleNamespace(
        client=client, sessions=fake_sessions, chat=stub_chat, upload_dir=tmp_path
    )


def _dev(uid: str) -> dict:
    return {"X-Device-ID": uid}


def _bearer(uid: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user_id=uid)}"}


# ---------------------------------------------------------------------------
# A1 · 会话详情 / 删除 / 重命名（审计点名的 IDOR 面）
# ---------------------------------------------------------------------------


class TestSessionDetailOwnership:
    def test_cross_user_404_same_user_200_anonymous_unchanged(self, api):
        api.sessions.seed("p48-s1", "alice")
        # 跨用户：404（防存在性枚举）
        assert (
            api.client.get("/api/v1/sessions/p48-s1", headers=_dev("bob")).status_code
            == 404
        )
        # 同用户：200
        resp = api.client.get("/api/v1/sessions/p48-s1", headers=_dev("alice"))
        assert resp.status_code == 200
        assert resp.json()["id"] == "p48-s1"
        # 匿名（无身份声明）：行为不变（strangler 纪律）
        assert api.client.get("/api/v1/sessions/p48-s1").status_code == 200

    def test_legacy_anonymous_bucket_visible_to_any_identity(self, api):
        """前身份期 default_user 桶：任何声明身份都可见（迁移不破坏）。"""
        api.sessions.seed("p48-legacy", "default_user")
        assert (
            api.client.get("/api/v1/sessions/p48-legacy", headers=_dev("bob")).status_code
            == 200
        )

    def test_delete_cross_user_404_same_user_204(self, api):
        api.sessions.seed("p48-d1", "alice")
        assert (
            api.client.delete("/api/v1/sessions/p48-d1", headers=_dev("bob")).status_code
            == 404
        )
        assert api.sessions.rows["p48-d1"].is_active, "跨用户 404 必须不误删"
        assert (
            api.client.delete("/api/v1/sessions/p48-d1", headers=_dev("alice")).status_code
            == 204
        )
        assert not api.sessions.rows["p48-d1"].is_active

    def test_patch_cross_user_404_and_identity_forced(self, api):
        api.sessions.seed("p48-p1", "alice")
        resp = api.client.patch(
            "/api/v1/sessions/p48-p1",
            json={"title": "hijack"},
            headers=_dev("bob"),
        )
        assert resp.status_code == 404
        assert api.sessions.rows["p48-p1"].title == "t"

        # 持身份 PATCH 改 user_id → 以身份为准（body 写 bob 也落 alice）
        resp = api.client.patch(
            "/api/v1/sessions/p48-p1",
            json={"title": "mine", "user_id": "bob"},
            headers=_dev("alice"),
        )
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "alice"
        assert resp.json()["title"] == "mine"


# ---------------------------------------------------------------------------
# A1 · 列表 / 计数 / 创建（身份覆盖 user_id）
# ---------------------------------------------------------------------------


class TestSessionIdentityScoping:
    def test_list_cross_user_404_same_user_200(self, api):
        api.sessions.seed("p48-l1", "alice")
        assert (
            api.client.get(
                "/api/v1/sessions/", params={"user_id": "alice"}, headers=_dev("bob")
            ).status_code
            == 404
        )
        resp = api.client.get(
            "/api/v1/sessions/", params={"user_id": "alice"}, headers=_dev("alice")
        )
        assert resp.status_code == 200
        assert [s["id"] for s in resp.json()] == ["p48-l1"]
        # 匿名查询保持现状
        assert (
            api.client.get("/api/v1/sessions/", params={"user_id": "alice"}).status_code
            == 200
        )

    def test_count_cross_user_404_same_user_200(self, api):
        api.sessions.seed("p48-n1", "alice")
        assert (
            api.client.get(
                "/api/v1/sessions/user/alice/count", headers=_dev("bob")
            ).status_code
            == 404
        )
        resp = api.client.get(
            "/api/v1/sessions/user/alice/count", headers=_dev("alice")
        )
        assert resp.status_code == 200
        assert resp.json()["session_count"] == 1

    def test_create_forces_caller_identity_over_body(self, api):
        """声明身份创建会话：body 里的他人 user_id 被身份覆盖。"""
        resp = api.client.post(
            "/api/v1/sessions/",
            json={"id": "p48-c1", "title": "t", "user_id": "bob"},
            headers=_dev("alice"),
        )
        assert resp.status_code == 201
        assert resp.json()["user_id"] == "alice"
        assert api.sessions.rows["p48-c1"].user_id == "alice"

    def test_create_without_identity_keeps_body_user_id(self, api):
        resp = api.client.post(
            "/api/v1/sessions/",
            json={"id": "p48-c2", "title": "t", "user_id": "carol"},
        )
        assert resp.status_code == 201
        assert resp.json()["user_id"] == "carol"


# ---------------------------------------------------------------------------
# A1 · 写路径（chat 流式 / 消息投递 / 历史 / 清空）
# ---------------------------------------------------------------------------


class TestWritePathOwnership:
    def test_chat_stream_cross_user_404_same_user_200(self, api):
        api.chat.owners["p48-w1"] = "alice"
        resp = api.client.post(
            "/api/v1/chat/stream",
            json={"message": "hi", "session_id": "p48-w1", "stream": True},
            headers=_dev("bob"),
        )
        assert resp.status_code == 404
        assert not api.chat.save_calls, "跨用户写必须在业务链路之前被拦下"

        resp = api.client.post(
            "/api/v1/chat/stream",
            json={"message": "hi", "session_id": "p48-w1", "stream": True},
            headers=_dev("alice"),
        )
        assert resp.status_code == 200

    def test_add_message_cross_user_404_same_user_201(self, api):
        api.sessions.seed("p48-m1", "alice")
        payload = {
            "session_id": "p48-m1",
            "message_type": "user_query",
            "content": "hello",
            "order_index": 0,
        }
        assert (
            api.client.post(
                "/api/v1/sessions/p48-m1/messages", json=payload, headers=_dev("bob")
            ).status_code
            == 404
        )
        assert (
            api.client.post(
                "/api/v1/sessions/p48-m1/messages", json=payload, headers=_dev("alice")
            ).status_code
            == 201
        )

    def test_history_cross_user_404_same_user_200(self, api):
        api.chat.owners["p48-h1"] = "alice"
        assert (
            api.client.get("/api/v1/chat/history/p48-h1", headers=_dev("bob")).status_code
            == 404
        )
        assert (
            api.client.get("/api/v1/chat/history/p48-h1", headers=_dev("alice")).status_code
            == 200
        )
        # 不存在的会话：404（与跨用户同款，不泄露差异）
        assert (
            api.client.get("/api/v1/chat/history/ghost", headers=_dev("alice")).status_code
            == 404
        )

    def test_clear_session_cross_user_404(self, api):
        api.chat.owners["p48-cl1"] = "alice"
        assert (
            api.client.delete("/api/v1/chat/session/p48-cl1", headers=_dev("bob")).status_code
            == 404
        )
        assert (
            api.client.delete(
                "/api/v1/chat/session/p48-cl1", headers=_dev("alice")
            ).status_code
            == 200
        )


# ---------------------------------------------------------------------------
# A1 · JWT regime：身份 = JWT sub（服务端签发）
# ---------------------------------------------------------------------------


class TestJwtRegimeIdentity:
    @pytest.fixture(autouse=True)
    def _jwt_on(self, monkeypatch):
        monkeypatch.setattr(settings, "V1_REQUIRE_JWT", True)

    def test_missing_token_401(self, api):
        api.sessions.seed("p48-j1", "alice")
        assert api.client.get("/api/v1/sessions/p48-j1").status_code == 401

    def test_header_cannot_forge_identity(self, api):
        """JWT regime 下 X-Device-ID 不参与身份——只有 sub 算数。"""
        api.sessions.seed("p48-j1", "alice")
        resp = api.client.get(
            "/api/v1/sessions/p48-j1", headers=_dev("alice")
        )
        assert resp.status_code == 401  # 无 token，头不算数

    def test_sub_owner_200_sub_other_404(self, api):
        api.sessions.seed("p48-j1", "alice")
        resp = api.client.get("/api/v1/sessions/p48-j1", headers=_bearer("alice"))
        assert resp.status_code == 200
        assert (
            api.client.get("/api/v1/sessions/p48-j1", headers=_bearer("bob")).status_code
            == 404
        )

    def test_sub_overrides_query_user_id_on_list(self, api):
        api.sessions.seed("p48-j2", "alice")
        # sub=alice 查 bob 的列表 → 404（身份覆盖 query）
        assert (
            api.client.get(
                "/api/v1/sessions/", params={"user_id": "bob"}, headers=_bearer("alice")
            ).status_code
            == 404
        )
        assert (
            api.client.get(
                "/api/v1/sessions/", params={"user_id": "alice"}, headers=_bearer("alice")
            ).status_code
            == 200
        )


# ---------------------------------------------------------------------------
# A2 · 上传归属（sidecar 读门禁）
# ---------------------------------------------------------------------------


def _upload_file(api, name="p48.txt", data=b"hello-a2", headers=None):
    return api.client.post(
        "/api/v1/upload/file",
        files={"file": (name, data, "text/plain")},
        headers=headers or {},
    )


class TestUploadOwnership:
    def test_upload_records_owner_and_gates_read(self, api):
        resp = _upload_file(api, headers=_dev("alice"))
        assert resp.status_code == 200
        filename = resp.json()["filename"]
        sidecar = api.upload_dir / ".owners" / f"{filename}.owner"
        assert sidecar.exists()
        assert sidecar.read_text(encoding="utf-8") == "alice"

        # 归属人可读
        got = api.client.get(
            f"/api/v1/upload/files/{filename}", headers=_dev("alice")
        )
        assert got.status_code == 200
        assert got.content == b"hello-a2"
        # 跨身份读 → 404；匿名读有主文件 → 404
        assert (
            api.client.get(f"/api/v1/upload/files/{filename}", headers=_dev("bob")).status_code
            == 404
        )
        assert api.client.get(f"/api/v1/upload/files/{filename}").status_code == 404

    def test_legacy_file_without_sidecar_readable_anonymously(self, api):
        """无 sidecar = 历史/匿名文件：放行（现状不变）。"""
        (api.upload_dir / "legacy-p48.txt").write_bytes(b"old")
        assert api.client.get("/api/v1/upload/files/legacy-p48.txt").status_code == 200

    def test_image_read_gated_same_as_file(self, api):
        resp = api.client.post(
            "/api/v1/upload/image",
            files={"image": ("p48.png", b"\x89PNG", "image/png")},
            headers=_dev("alice"),
        )
        assert resp.status_code == 200
        filename = resp.json()["filename"]
        assert (
            api.client.get(f"/api/v1/upload/images/{filename}", headers=_dev("bob")).status_code
            == 404
        )
        assert (
            api.client.get(
                f"/api/v1/upload/images/{filename}", headers=_dev("alice")
            ).status_code
            == 200
        )

    def test_delete_cross_identity_404_then_owner_200(self, api):
        resp = _upload_file(api, headers=_dev("alice"))
        assert resp.status_code == 200
        file_id = resp.json()["file_id"]

        assert (
            api.client.delete(f"/api/v1/upload/{file_id}", headers=_dev("bob")).status_code
            == 404
        )
        leftovers = list(api.upload_dir.rglob(f"{file_id}_*"))
        assert leftovers, "跨身份删除不得真的删掉文件"

        assert (
            api.client.delete(f"/api/v1/upload/{file_id}", headers=_dev("alice")).status_code
            == 200
        )
        assert not list(api.upload_dir.rglob(f"{file_id}_*"))

    def test_flag_on_read_requires_token_and_matches_owner(self, api, monkeypatch):
        """JWT regime 下读也要凭证，且 owner=token sub。"""
        monkeypatch.setattr(settings, "V1_REQUIRE_JWT", True)
        # 无凭证读 → 401（审计 A2：写读同口径）
        resp = _upload_file(api, headers=_bearer("alice"))
        assert resp.status_code == 200
        filename = resp.json()["filename"]
        assert (
            api.client.get(f"/api/v1/upload/files/{filename}").status_code == 401
        )
        # 同 sub 可读、异 sub 404
        assert (
            api.client.get(
                f"/api/v1/upload/files/{filename}", headers=_bearer("alice")
            ).status_code
            == 200
        )
        assert (
            api.client.get(
                f"/api/v1/upload/files/{filename}", headers=_bearer("bob")
            ).status_code
            == 404
        )
