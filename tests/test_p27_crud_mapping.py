"""P2-6 (T1) — CRUDBase.create 放宽为 Mapping 入参的守卫测试。

目标：infrastructure 层不再强制要求 interfaces 的 HTTP pydantic 模型作为
create 入参（dict / TypedDict 等任意 Mapping 均可），同时 pydantic 入参行为
零回归（jsonable_encoder 对 Mapping 恒等传递）。

运行：./.venv/Scripts/python.exe -m pytest tests/test_p27_crud_mapping.py -q
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agentix.infrastructure.core.database import Base
from agentix.infrastructure.persistence.crud.crud_chat_message import (
    chat_message,
)
from agentix.infrastructure.persistence.crud.crud_chat_session import chat_session
from agentix.infrastructure.persistence.db.models.chat_message import (  # noqa: F401
    ChatMessage,
    ChatSessionSnapshot,
)
from agentix.infrastructure.persistence.db.models.chat_session import (  # noqa: F401
    ChatSession,
)
from agentix.interfaces.http.models.chat_message import ChatMessageCreate
from agentix.interfaces.http.models.chat_session import ChatSessionCreate


@pytest.fixture()
def db():
    """每条测试独立的 sqlite 内存库（StaticPool 保证同连接）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


class TestCreateAcceptsMapping:
    """T1 核心行为：Mapping 入参建行成功。"""

    def test_create_accepts_plain_dict(self, db):
        created = chat_session.create(
            db, obj_in={"id": "sess-dict-1", "title": "dict 入参会话"}
        )
        assert created.id == "sess-dict-1"
        assert created.title == "dict 入参会话"
        assert created.is_active is True

    def test_create_accepts_typed_dict(self, db):
        """domain 层将用 stdlib TypedDict 作为 CRUD 载体（P2-6 决策）。"""

        class SessionPayload(dict):
            """模拟 TypedDict（运行时即 dict 子类）。"""

        payload = SessionPayload(id="sess-td-1", title="typed dict 会话")
        created = chat_session.create(db, obj_in=payload)
        assert created.id == "sess-td-1"
        assert created.title == "typed dict 会话"

    def test_create_mapping_extra_keys_surface_as_model_error(self, db):
        """Mapping 不经 pydantic 校验：错误键直接暴露为模型构造错误。

        这是设计上的取舍（与 pydantic 入参的「静默忽略额外字段」不同），
        测试锁定该语义，防止未来有人在 create 内偷偷加校验层。
        """
        from sqlalchemy.exc import StatementError

        with pytest.raises((StatementError, TypeError, ValueError)):
            chat_session.create(
                db, obj_in={"id": "sess-bad", "title": "x", "nonexistent_field": 1}
            )


class TestCreatePydanticRegression:
    """回归：pydantic 入参行为与 T1 之前完全一致。"""

    def test_create_accepts_pydantic_session(self, db):
        created = chat_session.create(
            db,
            obj_in=ChatSessionCreate(id="sess-pyd-1", title="pydantic 会话"),
        )
        assert created.id == "sess-pyd-1"
        assert created.title == "pydantic 会话"

    def test_create_accepts_pydantic_message(self, db):
        chat_session.create(db, obj_in={"id": "sess-pyd-2", "title": "t"})
        created = chat_message.create(
            db,
            obj_in=ChatMessageCreate(
                session_id="sess-pyd-2",
                message_type="user_query",
                content="你好",
                order_index=0,
            ),
        )
        assert created.session_id == "sess-pyd-2"
        assert created.content == "你好"

    def test_create_mapping_message_equivalent_to_pydantic(self, db):
        """同一数据 dict 与 pydantic 两条路径产出的行字段一致。"""
        chat_session.create(db, obj_in={"id": "sess-eq", "title": "t"})
        via_dict = chat_message.create(
            db,
            obj_in={
                "session_id": "sess-eq",
                "message_type": "user_query",
                "content": "同数据",
                "order_index": 1,
            },
        )
        via_pyd = chat_message.create(
            db,
            obj_in=ChatMessageCreate(
                session_id="sess-eq",
                message_type="user_query",
                content="同数据",
                order_index=2,
            ),
        )
        for field in ("session_id", "message_type", "content"):
            assert getattr(via_dict, field) == getattr(via_pyd, field)
