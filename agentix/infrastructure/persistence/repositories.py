"""P2-6 (T6) — 持久化 Repository 默认实现。

自 ``application/services/chat_service.py`` 下沉：这两个类本来就是
**infrastructure 侧的实现**（满足「infrastructure 实现 domain.ports」的
方向约束，方案 2.2 规则 4），放在 application 层属于归置错误——application
应依赖 ``domain.ports`` 抽象，具体 CRUD 包装属于基础设施职责。

契约：``SessionRepositoryPort`` / ``MessageRepositoryPort``（domain.ports）。
载体：P2-6 (T4) 起为 ``domain.ports.payloads`` 的 TypedDict（Mapping 兼容）。
"""
from sqlalchemy.orm import Session

from agentix.domain.ports import MessageCreatePayload, SessionCreatePayload
from agentix.infrastructure.persistence.crud import chat_message, chat_session


class SessionRepository:
    """会话存储访问（默认 CRUD 实现，符合 SessionRepositoryPort）。"""

    def get(self, db: Session, *, id: str):
        return chat_session.get(db, id=id)

    def create(self, db: Session, *, obj_in: SessionCreatePayload):
        return chat_session.create(db, obj_in=obj_in)

    def update_activity(self, db: Session, *, session_id: str):
        return chat_session.update_activity(db, session_id=session_id)


class MessageRepository:
    """消息存储访问（默认 CRUD 实现）。"""

    def get_latest_by_session(self, db: Session, *, session_id: str):
        return chat_message.get_latest_by_session(db, session_id=session_id)

    def create(self, db: Session, *, obj_in: MessageCreatePayload):
        return chat_message.create(db, obj_in=obj_in)

    def get_by_session(
        self, db: Session, *, session_id: str, skip: int = 0, limit: int = 100
    ):
        return chat_message.get_by_session(
            db, session_id=session_id, skip=skip, limit=limit
        )

    def delete_by_session(self, db: Session, *, session_id: str) -> int:
        return chat_message.delete_by_session(db, session_id=session_id)


__all__ = ["SessionRepository", "MessageRepository"]
