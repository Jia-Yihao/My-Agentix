"""聊天 API 离线契约测试（接口审查批次 2026-09-28 全量重写）。

**原文件的问题**（「看起来像测试的脚本」）：
- 两个用例**零 assert**（全是 print）→ 必然通过，给人端到端已覆盖的假象；
- 断言/读取 `msg.get("is_user")`、`msg.get("message")`——实际契约是
  ``message_type`` / ``content``，字段名全错；
- 第 129 行在 ``async with httpx.AsyncClient`` **退出之后**使用 session
  → 运行时必抛 closed client，被 except 吞掉只打印；
- 依赖 ``localhost:8000`` 真实服务，CI 被 ``integration`` 排除。

现改为**离线** TestClient 契约测试（mini app + stub ChatService，零外部依赖），
钉死接口审查批次修复的契约点：
1. ``/stream`` 流式主路径**落库助手消息**（原从不落库 → 刷新丢回答/引用）；
2. ``/history`` 走最近 N 条窗口 + ``limit`` 有下界（ge=1, le=100）；
3. 非流式响应不外泄 ``agent_state``（与落库过滤口径对齐）；
4. legacy GET /chat/stream 长度上限与 ``settings.QUERY_MAX_LENGTH`` 同源；
5. CRUDBase 层 ``get_recent_by_session`` 窗口方向（最新 N 条、正序返回）；
6. ``count_by_user`` 默认口径排除软删会话（与 get_by_user 对齐）。
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import agentix.interfaces.http.v1.chat as chat_module
from agentix.config import settings


# ---------------------------------------------------------------------------
# Stub ChatService（模块级单例被 monkeypatch 替换；零 DB / 零 LLM）
# ---------------------------------------------------------------------------


class StubChatService:
    """只记录调用 + 按脚本产出事件的最小替身。"""

    def __init__(self, events=None):
        self.events = events
        self.saved: list[dict] = []
        self.recent_calls: list[dict] = []
        self.ask_calls = 0

    def get_or_create_session(self, db, session_id, user_id):
        return session_id or "sess-1"

    def session_exists(self, db, session_id):
        return session_id == "sess-1"

    def get_session_owner(self, db, session_id):
        # 审计批次 A1：sess-1 是前身份期匿名桶会话；其余不存在 → None
        return "default_user" if session_id == "sess-1" else None

    async def save_message(
        self, db, session_id, message, is_user, route=None, metadata=None
    ):
        self.saved.append(
            {
                "session_id": session_id,
                "message": message,
                "is_user": is_user,
                "route": route,
                "metadata": metadata,
            }
        )
        return f"m-{len(self.saved)}"

    async def stream_events(
        self, message, session_id, image_path=None, file_path=None,
        ingest_incremental=None,
    ):
        for event in (
            self.events
            if self.events is not None
            else [
                {"type": "metadata", "metadata": {"status": "processing"},
                 "session_id": session_id},
                {"type": "message", "content": "你", "session_id": session_id},
                {"type": "message", "content": "好", "session_id": session_id},
                {"type": "done",
                 "metadata": {"sources": [{"law": "民法典"}]},
                 "session_id": session_id},
            ]
        ):
            yield dict(event)

    async def ask(self, message, session_id, image_path=None, file_path=None,
                  ingest_incremental=None):
        self.ask_calls += 1
        return {
            "message": "非流式回答",
            "session_id": session_id,
            "route": "kb-query",
            "route_logic": "命中知识库",
            "sources": [{"law": "民法典"}],
            "metadata": {"agent_state": {"huge": "graph-state"}, "foo": 1},
            "status": "ok",
            "error_code": None,
        }

    def list_recent_messages(self, db, *, session_id, skip=0, limit=100):
        self.recent_calls.append({"skip": skip, "limit": limit})
        return []


@pytest.fixture()
def stub_service(monkeypatch):
    stub = StubChatService()
    monkeypatch.setattr(chat_module, "_chat_service", stub)
    return stub


@pytest.fixture()
def client(stub_service):
    app = FastAPI()
    app.include_router(chat_module.router, prefix="/api/v1/chat")
    return TestClient(app)


def _sse_frames(body: str) -> list[dict]:
    frames = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block.startswith("data: "):
            continue
        frames.append(json.loads(block[len("data: "):]))
    return frames


# ---------------------------------------------------------------------------
# 1. 流式主路径落库（原 bug：刷新后只剩用户提问）
# ---------------------------------------------------------------------------


class TestStreamPersistence:
    def test_stream_saves_user_and_assistant_messages(self, client, stub_service):
        resp = client.post(
            "/api/v1/chat/stream", json={"message": "你好呀", "stream": True}
        )
        assert resp.status_code == 200

        frames = _sse_frames(resp.text)
        assert [f["type"] for f in frames] == ["metadata", "message", "message", "done"]

        user_saves = [s for s in stub_service.saved if s["is_user"]]
        assistant_saves = [s for s in stub_service.saved if not s["is_user"]]
        assert len(user_saves) == 1
        assert len(assistant_saves) == 1, "流式主路径必须落库助手消息（历史回放契约）"
        assert assistant_saves[0]["message"] == "你好"
        assert assistant_saves[0]["metadata"] == {"sources": [{"law": "民法典"}]}
        assert assistant_saves[0]["session_id"] == "sess-1"

    def test_stream_persists_partial_answer_on_error_path(
        self, monkeypatch
    ):
        stub = StubChatService(
            events=[
                {"type": "metadata", "metadata": {"status": "processing"},
                 "session_id": "sess-1"},
                {"type": "message", "content": "部分", "session_id": "sess-1"},
                # 后端契约：error 之后必补 done（test_p14 钉死）
                {"type": "error", "content": "出错了", "session_id": "sess-1"},
                {"type": "done", "metadata": {"sources": []},
                 "session_id": "sess-1"},
            ]
        )
        monkeypatch.setattr(chat_module, "_chat_service", stub)
        app = FastAPI()
        app.include_router(chat_module.router, prefix="/api/v1/chat")
        client = TestClient(app)

        resp = client.post("/api/v1/chat/stream", json={"message": "你好"})
        assert resp.status_code == 200
        frames = _sse_frames(resp.text)
        assert [f["type"] for f in frames] == [
            "metadata", "message", "error", "done"
        ]

        assistant_saves = [s for s in stub.saved if not s["is_user"]]
        assert len(assistant_saves) == 1
        # 已产出的 token 不因报错丢弃（客户端断连前的部分回答同样保留）
        assert assistant_saves[0]["message"] == "部分"


# ---------------------------------------------------------------------------
# 2. 历史回放：最近 N 条窗口 + limit 下界
# ---------------------------------------------------------------------------


class TestHistoryEndpoint:
    def test_history_uses_recent_window(self, client, stub_service):
        resp = client.get("/api/v1/chat/history/sess-1?limit=50")
        assert resp.status_code == 200
        assert resp.json() == []
        assert stub_service.recent_calls == [{"skip": 0, "limit": 50}]

    @pytest.mark.parametrize("bad_limit", [0, -1, 101])
    def test_history_limit_bounds(self, client, bad_limit):
        # 原实现无 ge=1：limit=-1 在 SQLite 等价全量（PG 直接语法错 500），
        # limit=0 返回空数组让前端静默不恢复
        resp = client.get(f"/api/v1/chat/history/sess-1?limit={bad_limit}")
        assert resp.status_code == 422

    def test_history_unknown_session_404(self, client):
        resp = client.get("/api/v1/chat/history/not-a-session")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 3. 非流式响应不外泄 agent_state
# ---------------------------------------------------------------------------


class TestNonStreamMetadata:
    def test_agent_state_filtered_from_response(self, client):
        resp = client.post(
            "/api/v1/chat",
            json={"message": "你好", "session_id": "sess-1", "stream": False},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "agent_state" not in (data.get("metadata") or {})
        assert data["metadata"]["foo"] == 1
        assert data["status"] == "ok"


# ---------------------------------------------------------------------------
# 4. legacy GET /chat/stream 长度上限与配置同源
# ---------------------------------------------------------------------------


class TestLegacyGetStreamLimit:
    def test_message_over_limit_rejected(self, client):
        too_long = "x" * (settings.QUERY_MAX_LENGTH + 1)
        resp = client.get(
            "/api/v1/chat/chat/stream", params={"message": too_long}
        )
        assert resp.status_code == 422

    def test_message_at_limit_accepted(self, client, stub_service):
        exact = "x" * settings.QUERY_MAX_LENGTH
        resp = client.get(
            "/api/v1/chat/chat/stream", params={"message": exact}
        )
        assert resp.status_code == 200
        assert stub_service.saved  # 用户消息已落库


# ---------------------------------------------------------------------------
# 5. CRUD：最近 N 条窗口方向（真实 SQLite）
# ---------------------------------------------------------------------------


class TestCrudRecentWindow:
    @pytest.fixture()
    def db(self):
        from sqlalchemy.orm import sessionmaker

        from agentix.infrastructure.core.database import Base, engine
        import agentix.infrastructure.persistence.db.models  # noqa: F401

        Base.metadata.create_all(bind=engine)
        TestSession = sessionmaker(bind=engine)
        session = TestSession()
        yield session
        session.rollback()
        session.close()

    def test_recent_window_returns_newest_ascending(self, db):
        from agentix.infrastructure.persistence.crud import chat_message

        sid = "crud-recent-window-test"
        chat_message.delete_by_session(db, session_id=sid)
        for i in range(1, 6):
            chat_message.create(
                db,
                obj_in={
                    "session_id": sid,
                    "message_type": "user_query" if i % 2 else "agent_response",
                    "content": f"msg-{i}",
                    "message_metadata": None,
                    "order_index": i,
                },
            )

        try:
            # 最新 2 条、正序（原 get_by_session 是「最旧 2 条」→ 会话超长丢最新）
            recent = chat_message.get_recent_by_session(db, session_id=sid, limit=2)
            assert [m.content for m in recent] == ["msg-4", "msg-5"]

            # skip 从最新往回数
            window = chat_message.get_recent_by_session(
                db, session_id=sid, skip=1, limit=2
            )
            assert [m.content for m in window] == ["msg-3", "msg-4"]

            # 旧方法语义保持不变（其他调用方不受影响）
            oldest = chat_message.get_by_session(db, session_id=sid, limit=2)
            assert [m.content for m in oldest] == ["msg-1", "msg-2"]
        finally:
            chat_message.delete_by_session(db, session_id=sid)


# ---------------------------------------------------------------------------
# 6. count_by_user 默认排除软删（与 get_by_user 口径对齐）
# ---------------------------------------------------------------------------


class TestSessionCountScope:
    @pytest.fixture()
    def db(self):
        from sqlalchemy.orm import sessionmaker

        from agentix.infrastructure.core.database import Base, engine
        import agentix.infrastructure.persistence.db.models  # noqa: F401

        Base.metadata.create_all(bind=engine)
        TestSession = sessionmaker(bind=engine)
        session = TestSession()
        yield session
        session.rollback()
        session.close()

    def test_count_excludes_soft_deleted_by_default(self, db):
        from agentix.domain.ports.payloads import SessionCreatePayload
        from agentix.infrastructure.persistence.crud import chat_session

        uid = "count-scope-test-user"
        # 清理历史残留（同一共享内存库）
        for s in chat_session.get_by_user(
            db, user_id=uid, active_only=False, limit=100
        ):
            chat_session.delete(db, id=s.id)

        active_ids, deleted_ids = [], []
        for i in range(2):
            sid = f"count-scope-active-{i}"
            chat_session.create(
                db,
                obj_in=SessionCreatePayload(
                    id=sid, user_id=uid, title=f"会话{i}"
                ),
            )
            active_ids.append(sid)
        for i in range(3):
            sid = f"count-scope-deleted-{i}"
            chat_session.create(
                db,
                obj_in=SessionCreatePayload(
                    id=sid, user_id=uid, title=f"删{i}"
                ),
            )
            chat_session.soft_delete(db, session_id=sid)
            deleted_ids.append(sid)

        try:
            assert chat_session.count_by_user(db, user_id=uid) == 2
            assert (
                chat_session.count_by_user(db, user_id=uid, active_only=False) == 5
            )
            # 与列表口径一致：计数不小于列表、活跃口径下二者相等
            active_list = chat_session.get_by_user(db, user_id=uid, limit=100)
            assert chat_session.count_by_user(db, user_id=uid) == len(active_list)
        finally:
            for sid in active_ids + deleted_ids:
                chat_session.delete(db, id=sid)
