"""P4（岗位对照·异常闭环 4.4）：失败 turn durable 边界（``FAILURE_BOUNDARY_ENABLED``）。

借鉴 hermes ``conversation_loop.py:1745`` durable failure boundary 与
``cli_session_mixin.py:753``「**先持久化后发布**」（source-code-study §5 4.4）：
失败的 turn 若不落一条 assistant 边界消息，历史里 user-user 相邻——刷新后
用户不知道刚才发生了什么，也没有任何可回放的失败痕迹。钉死契约：

1. **flag 治理**：``FAILURE_BOUNDARY_ENABLED`` 默认 False ⇒ 非流式 ask 失败
   与流式 error-only 的落库序列与基线逐位等价（零边界写入）；
2. **先持久化后发布**：非流式在 re-raise **之前**补写；流式在 yield error 帧
   **之前**补写（顺序：user → boundary → 发布）；
3. **排除码**：``context_overflow`` 不补写（该失败本质是上下文超限，补写
   只会复制用户已见过的信息）；未知 code ⇒ ``unknown_error``；
4. **正文已产出则不补写**：流式 partial-answer + error 走 finally 落库，
   不叠加边界（一条失败 turn 只落一条 assistant 消息）；``boundary_saved``
   防止同一 turn 多个 error 帧重复补写；
5. **补写自身失败绝不抛出**（边界是一致性修复，不覆盖原始错误路径）。
"""
from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient

import agentix.interfaces.http.v1.chat as chat_module
from agentix.application.services.breakpoint_resume import (
    FAILURE_BOUNDARY_EXCLUDED_CODES,
    failure_boundary_content,
)
from agentix.config import settings

# ---------------------------------------------------------------------------
# 基线断言：flag 默认关（flag 治理；见 配置管理.md §5）
# ---------------------------------------------------------------------------

assert settings.FAILURE_BOUNDARY_ENABLED is False, "P4 基线：失败边界默认关"


# ---------------------------------------------------------------------------
# Stub ChatService（模块级单例被 monkeypatch 替换；零 DB / 零 LLM）
# ---------------------------------------------------------------------------


class StubChatService:
    """记录调用 + 按脚本产出事件/异常的最小替身。"""

    def __init__(self, events=None, ask_exc=None):
        self.events = events
        self.ask_exc = ask_exc
        self.saved: list[dict] = []

    def get_or_create_session(self, db, session_id, user_id):
        return session_id or "sess-1"

    def session_exists(self, db, session_id):
        return session_id == "sess-1"

    def get_session_owner(self, db, session_id):
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
        for event in self.events or []:
            yield dict(event)

    async def ask(self, message, session_id, image_path=None, file_path=None,
                  ingest_incremental=None):
        if self.ask_exc is not None:
            raise self.ask_exc
        return {
            "message": "回答",
            "session_id": session_id,
            "route": "kb-query",
            "route_logic": "命中知识库",
            "sources": None,
            "metadata": {},
            "status": "ok",
            "error_code": None,
        }


def _install(monkeypatch, stub) -> None:
    monkeypatch.setattr(chat_module, "_chat_service", stub)


def _client(*, raise_server_exceptions: bool = False) -> TestClient:
    app = FastAPI()
    app.include_router(chat_module.router, prefix="/api/v1/chat")
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _sse_frames(body: str) -> list[dict]:
    frames = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block.startswith("data: "):
            continue
        frames.append(json.loads(block[len("data: "):]))
    return frames


def _assistant_saves(stub: StubChatService) -> list[dict]:
    return [s for s in stub.saved if not s["is_user"]]


# ---------------------------------------------------------------------------
# 1. helper 契约（纯函数）
# ---------------------------------------------------------------------------


class TestHelperContract:
    def test_flag_default_off(self):
        assert settings.FAILURE_BOUNDARY_ENABLED is False

    def test_excluded_code_returns_none(self):
        assert failure_boundary_content("context_overflow") is None
        assert failure_boundary_content("context_overflow", fallback="x") is None
        assert "context_overflow" in FAILURE_BOUNDARY_EXCLUDED_CODES

    def test_format_with_fallback(self):
        content = failure_boundary_content("llm_upstream", fallback="上游超时")
        assert content == "[对话边界] 本轮请求失败（llm_upstream）：上游超时"

    def test_format_without_fallback(self):
        assert failure_boundary_content("llm_upstream") == (
            "[对话边界] 本轮请求失败（llm_upstream）：请求未完成"
        )

    def test_blank_code_maps_to_unknown_error(self):
        assert failure_boundary_content(None) == (
            "[对话边界] 本轮请求失败（unknown_error）：请求未完成"
        )
        assert failure_boundary_content("  ") == (
            "[对话边界] 本轮请求失败（unknown_error）：请求未完成"
        )


# ---------------------------------------------------------------------------
# 2. 非流式：ask 失败 → re-raise 前先补写边界
# ---------------------------------------------------------------------------


class TestNonStreamBoundary:
    def test_flag_off_no_boundary(self, monkeypatch):
        stub = StubChatService(
            ask_exc=HTTPException(
                status_code=500,
                detail={"error_code": "llm_upstream", "message": "上游失败"},
            )
        )
        _install(monkeypatch, stub)
        resp = _client().post(
            "/api/v1/chat", json={"message": "hi", "session_id": "sess-1"}
        )
        assert resp.status_code == 500, "异常照常向上抛（基线语义不变）"
        assert _assistant_saves(stub) == [], "flag-off：历史保持 user-user 相邻"
        assert len(stub.saved) == 1 and stub.saved[0]["is_user"] is True

    def test_flag_on_persists_boundary_before_reraise(self, monkeypatch):
        monkeypatch.setattr(settings, "FAILURE_BOUNDARY_ENABLED", True)
        stub = StubChatService(
            ask_exc=HTTPException(
                status_code=500,
                detail={"error_code": "llm_upstream", "message": "上游失败"},
            )
        )
        _install(monkeypatch, stub)
        resp = _client().post(
            "/api/v1/chat", json={"message": "hi", "session_id": "sess-1"}
        )
        assert resp.status_code == 500, "先持久化后发布：异常仍向外抛"
        # 顺序：user 先落，boundary 后落（ask 抛出前完成）
        assert stub.saved[0]["is_user"] is True
        boundary = stub.saved[1]
        assert boundary["is_user"] is False
        assert boundary["route"] == "error"
        assert boundary["message"] == "[对话边界] 本轮请求失败（llm_upstream）：上游失败"
        assert boundary["metadata"]["failure_boundary"] is True
        assert boundary["metadata"]["error_code"] == "llm_upstream"
        assert boundary["metadata"]["http_status"] == 500

    def test_flag_on_excluded_code_skips(self, monkeypatch):
        monkeypatch.setattr(settings, "FAILURE_BOUNDARY_ENABLED", True)
        stub = StubChatService(
            ask_exc=HTTPException(
                status_code=500,
                detail={"error_code": "context_overflow", "message": "上下文超限"},
            )
        )
        _install(monkeypatch, stub)
        resp = _client().post(
            "/api/v1/chat", json={"message": "hi", "session_id": "sess-1"}
        )
        assert resp.status_code == 500
        assert _assistant_saves(stub) == [], "排除码不补写"

    def test_flag_on_string_detail_maps_unknown_error(self, monkeypatch):
        monkeypatch.setattr(settings, "FAILURE_BOUNDARY_ENABLED", True)
        stub = StubChatService(ask_exc=HTTPException(status_code=500, detail="boom"))
        _install(monkeypatch, stub)
        resp = _client().post(
            "/api/v1/chat", json={"message": "hi", "session_id": "sess-1"}
        )
        assert resp.status_code == 500
        boundary = _assistant_saves(stub)[0]
        assert boundary["message"] == "[对话边界] 本轮请求失败（unknown_error）：boom"
        assert boundary["metadata"]["error_code"] == "unknown_error"
        assert boundary["metadata"]["http_status"] == 500


# ---------------------------------------------------------------------------
# 3. 流式：error-only → yield 前先补写；正文已产出则不叠加
# ---------------------------------------------------------------------------


class TestStreamBoundary:
    def _post(self, stub) -> tuple:
        return _client().post(
            "/api/v1/chat/stream", json={"message": "hi", "session_id": "sess-1"}
        )

    def test_flag_on_error_only_persists_boundary(self, monkeypatch):
        monkeypatch.setattr(settings, "FAILURE_BOUNDARY_ENABLED", True)
        stub = StubChatService(
            events=[
                {"type": "error", "content": "出错了", "session_id": "sess-1"},
            ]
        )
        _install(monkeypatch, stub)
        resp = self._post(stub)
        assert resp.status_code == 200
        frames = _sse_frames(resp.text)
        assert [f["type"] for f in frames] == ["error"]
        assistant = _assistant_saves(stub)
        assert len(assistant) == 1, "error-only：finally 不落库，边界是唯一助手消息"
        assert assistant[0]["route"] == "error"
        assert assistant[0]["message"] == "[对话边界] 本轮请求失败（unknown_error）：出错了"
        assert assistant[0]["metadata"]["failure_boundary"] is True
        assert assistant[0]["metadata"]["error_code"] == "unknown_error"
        assert "http_status" not in assistant[0]["metadata"], "流式无 HTTP 状态语境"
        assert stub.saved[0]["is_user"] is True, "user 先于 boundary 落库"

    def test_flag_on_error_code_taken_from_event_metadata(self, monkeypatch):
        monkeypatch.setattr(settings, "FAILURE_BOUNDARY_ENABLED", True)
        stub = StubChatService(
            events=[
                {
                    "type": "error",
                    "content": "炸了",
                    "metadata": {"error_code": "llm_upstream"},
                    "session_id": "sess-1",
                },
            ]
        )
        _install(monkeypatch, stub)
        resp = self._post(stub)
        assert resp.status_code == 200
        boundary = _assistant_saves(stub)[0]
        assert boundary["metadata"]["error_code"] == "llm_upstream"
        assert "（llm_upstream）" in boundary["message"]

    def test_flag_on_double_error_single_boundary(self, monkeypatch):
        """boundary_saved 防重：同一 turn 多个 error 帧只补写一次。"""
        monkeypatch.setattr(settings, "FAILURE_BOUNDARY_ENABLED", True)
        stub = StubChatService(
            events=[
                {"type": "error", "content": "错一", "session_id": "sess-1"},
                {"type": "error", "content": "错二", "session_id": "sess-1"},
                {"type": "done", "session_id": "sess-1"},
            ]
        )
        _install(monkeypatch, stub)
        resp = self._post(stub)
        assert resp.status_code == 200
        assert [f["type"] for f in _sse_frames(resp.text)] == ["error", "error", "done"]
        assistant = _assistant_saves(stub)
        assert len(assistant) == 1
        assert "错一" in assistant[0]["message"], "首个 error 帧（含其正文）被捕获"

    def test_flag_on_partial_answer_no_boundary(self, monkeypatch):
        """正文已产出 ⇒ finally 落库 partial，不叠加边界（一条 turn 一条消息）。"""
        monkeypatch.setattr(settings, "FAILURE_BOUNDARY_ENABLED", True)
        stub = StubChatService(
            events=[
                {"type": "message", "content": "部分", "session_id": "sess-1"},
                {"type": "error", "content": "出错了", "session_id": "sess-1"},
                {"type": "done", "metadata": {"sources": []}, "session_id": "sess-1"},
            ]
        )
        _install(monkeypatch, stub)
        resp = self._post(stub)
        assert resp.status_code == 200
        assistant = _assistant_saves(stub)
        assert len(assistant) == 1
        assert assistant[0]["message"] == "部分"
        assert "failure_boundary" not in (assistant[0]["metadata"] or {})

    def test_flag_off_error_only_no_boundary(self, monkeypatch):
        stub = StubChatService(
            events=[
                {"type": "error", "content": "出错了", "session_id": "sess-1"},
            ]
        )
        _install(monkeypatch, stub)
        resp = self._post(stub)
        assert resp.status_code == 200
        assert _assistant_saves(stub) == [], "flag-off：落库序列与基线一致"
        assert len(stub.saved) == 1 and stub.saved[0]["is_user"] is True

    def test_flag_off_partial_answer_still_persisted(self, monkeypatch):
        """flag-off 回归：finally 的 partial 落库不受 P4 改动影响。"""
        stub = StubChatService(
            events=[
                {"type": "message", "content": "部分", "session_id": "sess-1"},
                {"type": "error", "content": "出错了", "session_id": "sess-1"},
                {"type": "done", "session_id": "sess-1"},
            ]
        )
        _install(monkeypatch, stub)
        resp = self._post(stub)
        assert resp.status_code == 200
        assistant = _assistant_saves(stub)
        assert len(assistant) == 1 and assistant[0]["message"] == "部分"
