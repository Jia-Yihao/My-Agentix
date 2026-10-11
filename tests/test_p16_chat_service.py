"""P1-6 ChatService 单测（业务逻辑从 API 层下沉）。

覆盖方案 P1-6 验收标准：
1. chat.py 无 ORM 调用（静态断言：源码不含 CRUD 单例引用）
2. ChatService 不启动 FastAPI 即可单测（内存假 repository 注入）
3. 行为保持：会话 get_or_create、消息 order_index 递增、metadata 过滤
   agent_state、save 失败回滚返回 None
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage

from agentix.application.services.chat_service import ChatService
from agentix.config import settings as settings_instance


# ---------------------------------------------------------------------------
# 内存假 repository（不触碰 SQLAlchemy）
# ---------------------------------------------------------------------------


class _FakeRow:
    """最小 ORM 行替身：动态属性 + 可控 id。"""

    def __init__(self, **kwargs):
        self.id = kwargs.pop("id", None) or f"row-{_FakeRow._counter}"
        _FakeRow._counter += 1
        self.order_index = kwargs.pop("order_index", 0)
        for k, v in kwargs.items():
            setattr(self, k, v)

    _counter = 1


class FakeSessionRepository:
    def __init__(self):
        self.rows: Dict[str, Any] = {}

    def get(self, db, *, id: str):
        return self.rows.get(id)

    def create(self, db, *, obj_in):
        row = _FakeRow(**dict(obj_in))
        self.rows[row.id] = row
        return row

    def update_activity(self, db, *, session_id: str):
        row = self.rows.get(session_id)
        if row:
            # 2026-09 修复：原为 ``datetime.utcnow()``，**与真实实现漂移**——
            # ``crud_chat_session.update_activity`` 已在 §7.7 改为带 tzinfo 的
            # UTC（该模型 ``updated_at`` 列声明 ``DateTime(timezone=True)``）。
            # 假体继续写 naive 值有两个后果：① 3.12+ 每次执行都抛
            # DeprecationWarning（全量测试的告警条数里就有它，且来源是**本仓
            # 自己的测试代码**而非第三方）；② 假体与实现语义不一致，
            # "测过了"其实测的不是同一件事。
            row.updated_at = datetime.now(timezone.utc)
        return row


class FakeMessageRepository:
    def __init__(self):
        self.rows: List[Any] = []
        self.fail_next_create = False

    def get_latest_by_session(self, db, *, session_id: str):
        found = [r for r in self.rows if r.session_id == session_id]
        return found[-1] if found else None

    def create(self, db, *, obj_in):
        if self.fail_next_create:
            self.fail_next_create = False
            raise RuntimeError("disk full")
        row = _FakeRow(**dict(obj_in))
        self.rows.append(row)
        return row

    def get_by_session(self, db, *, session_id: str, skip: int = 0, limit: int = 100):
        found = [r for r in self.rows if r.session_id == session_id]
        return found[skip : skip + limit]

    def delete_by_session(self, db, *, session_id: str) -> int:
        before = len(self.rows)
        self.rows = [r for r in self.rows if r.session_id != session_id]
        return before - len(self.rows)


def _make_service(**kwargs) -> ChatService:
    return ChatService(
        session_repo=kwargs.get("session_repo", FakeSessionRepository()),
        message_repo=kwargs.get("message_repo", FakeMessageRepository()),
        graph_getter=kwargs.get("graph_getter"),
    )


def _run(coro):
    return asyncio.run(coro)


class TestSessionOrchestration:
    def test_get_or_create_session_existing(self):
        svc = _make_service()
        db = MagicMock()
        sid = svc.get_or_create_session(db, None, "u1")
        again = svc.get_or_create_session(db, sid, "u1")
        assert again == sid  # 已存在 → 原样返回

    def test_get_or_create_session_nonexistent_id_creates_new(self):
        """传入的 session_id 不存在 → 新建（行为保持，非透传坏 id）。"""
        svc = _make_service()
        db = MagicMock()
        new_id = svc.get_or_create_session(db, "ghost-id", "u1")
        assert new_id != "ghost-id"
        assert svc._session_repo.get(db, id=new_id) is not None

    def test_session_exists(self):
        svc = _make_service()
        db = MagicMock()
        sid = svc.get_or_create_session(db, None, "u1")
        assert svc.session_exists(db, sid) is True
        assert svc.session_exists(db, "nope") is False


class TestMessageOrchestration:
    def test_save_message_order_index_increments(self):
        svc = _make_service()
        db = MagicMock()
        sid = svc.get_or_create_session(db, None, "u1")
        _run(svc.save_message(db, sid, "问题", is_user=True))
        _run(svc.save_message(db, sid, "答复", is_user=False))
        msgs = svc.list_messages(db, session_id=sid)
        assert [m.order_index for m in msgs] == [1, 2]

    def test_save_message_filters_agent_state(self):
        """metadata 中的 agent_state（完整图状态）不得持久化。"""
        svc = _make_service()
        db = MagicMock()
        sid = svc.get_or_create_session(db, None, "u1")
        _run(
            svc.save_message(
                db, sid, "答复", is_user=False,
                route="legal", metadata={"agent_state": {"huge": "state"}, "sources": [1]},
            )
        )
        msg = svc.list_messages(db, session_id=sid)[0]
        md = msg.message_metadata
        assert "agent_state" not in md
        assert md["sources"] == [1]
        assert md["route"] == "legal"

    def test_save_failure_rolls_back_and_returns_none(self):
        """写库失败：db.rollback() 被调用且返回 None（P0 接口修复行为保持）。"""
        svc = _make_service(message_repo=FakeMessageRepository())
        svc._message_repo.fail_next_create = True
        db = MagicMock()
        sid = svc.get_or_create_session(db, None, "u1")
        result = _run(svc.save_message(db, sid, "内容", is_user=True))
        assert result is None
        db.rollback.assert_called_once()

    def test_clear_session_messages(self):
        svc = _make_service()
        db = MagicMock()
        sid = svc.get_or_create_session(db, None, "u1")
        _run(svc.save_message(db, sid, "q", is_user=True))
        deleted = svc.clear_session_messages(db, session_id=sid)
        assert deleted == 1
        assert svc.list_messages(db, session_id=sid) == []


class TestAgentAsk:
    def _fake_graph(self, result=None, exc=None):
        class _FakeGraph:
            async def ainvoke(self, input_state, config=None):
                if exc is not None:
                    raise exc
                return result

        return _FakeGraph()

    def _ok_result(self, degraded=False):
        msg = AIMessage(content="答复内容")
        if degraded:
            msg.additional_kwargs["degraded"] = True
        return {
            "messages": [msg],
            "router": {"type": "legal", "logic": "test"},
            "sources": [],
        }

    def test_ask_success_path(self, monkeypatch):
        monkeypatch.setattr(settings_instance, "LEGACY_ERROR_MODE", False)
        svc = _make_service(graph_getter=lambda: self._fake_graph(result=self._ok_result()))
        result = _run(svc.ask("你好", "s1"))
        assert result["status"] == "ok"
        assert result["message"] == "答复内容"

    def test_ask_degraded_flag_propagates(self, monkeypatch):
        monkeypatch.setattr(settings_instance, "LEGACY_ERROR_MODE", False)
        svc = _make_service(
            graph_getter=lambda: self._fake_graph(result=self._ok_result(degraded=True))
        )
        result = _run(svc.ask("你好", "s1"))
        assert result["status"] == "degraded"

    def test_ask_string_sources_normalized(self, monkeypatch):
        monkeypatch.setattr(settings_instance, "LEGACY_ERROR_MODE", False)
        result_data = self._ok_result()
        result_data["sources"] = ["doc-a", "doc-b"]
        svc = _make_service(graph_getter=lambda: self._fake_graph(result=result_data))
        result = _run(svc.ask("你好", "s1"))
        assert result["sources"] == [
            {"document_id": "doc-a", "source": "doc-a"},
            {"document_id": "doc-b", "source": "doc-b"},
        ]


class TestApiLayerNoOrm:
    def test_chat_py_has_no_crud_imports(self):
        """方案 P1-6 验收 1：chat.py 不再引用任何 CRUD/ORM 编排。"""
        import inspect

        import agentix.interfaces.http.v1.chat as chat_module

        source = inspect.getsource(chat_module)
        assert "chat_message." not in source
        assert "chat_session." not in source
        assert "from agentix.infrastructure.persistence.crud import" not in source
