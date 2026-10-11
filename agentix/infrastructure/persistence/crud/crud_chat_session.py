"""
CRUD operations for ChatSession model.

Lightweight user ID system - group sessions by user_id without authentication.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy import and_, desc
from sqlalchemy.orm import Session

from agentix.infrastructure.persistence.crud.base import CRUDBase
from agentix.infrastructure.persistence.db.models.chat_session import ChatSession


# P2-6 (T3): 泛型参数从 interfaces 的 HTTP pydantic 契约改为 Mapping/Dict 载体。
# 创建/更新的实际载体由调用方决定（domain TypedDict / 普通 dict / HTTP 端点
# 的 model_dump() 结果），本层只要求 Mapping 协议。
class CRUDChatSession(CRUDBase[ChatSession, Mapping[str, Any], Dict[str, Any]]):
    """CRUD operations for ChatSession model."""

    def get_all(
        self,
        db: Session,
        *,
        skip: int = 0,
        limit: int = 100,
        active_only: bool = True
    ) -> List[ChatSession]:
        """
        Get all chat sessions (across all users).

        Args:
            db: Database session
            skip: Number of records to skip
            limit: Maximum number of records to return
            active_only: Only return active (non-deleted) sessions

        Returns:
            List of chat sessions
        """
        query = db.query(self.model)

        if active_only:
            query = query.filter(self.model.is_active == True)  # noqa: E712

        return query.order_by(desc(self.model.updated_at)).offset(skip).limit(limit).all()

    def get_by_user(
        self,
        db: Session,
        *,
        user_id: str,
        skip: int = 0,
        limit: int = 100,
        active_only: bool = True
    ) -> List[ChatSession]:
        """
        Get chat sessions for a specific user.

        Args:
            db: Database session
            user_id: User identifier (device ID, UUID, etc.)
            skip: Number of records to skip
            limit: Maximum number of records to return
            active_only: Only return active (non-deleted) sessions

        Returns:
            List of chat sessions for the user
        """
        query = db.query(self.model).filter(self.model.user_id == user_id)

        if active_only:
            query = query.filter(self.model.is_active == True)  # noqa: E712

        return query.order_by(desc(self.model.updated_at)).offset(skip).limit(limit).all()

    def get_with_messages(
        self, db: Session, *, session_id: str
    ) -> Optional[ChatSession]:
        """
        Get a chat session with its messages loaded.

        Args:
            db: Database session
            session_id: Session UUID

        Returns:
            ChatSession with messages relationship loaded, or None
        """
        return (
            db.query(self.model)
            .filter(
                and_(
                    self.model.id == session_id,
                    self.model.is_active == True  # noqa: E712
                )
            )
            .first()
        )

    def update_activity(
        self, db: Session, *, session_id: str
    ) -> Optional[ChatSession]:
        """
        Update the session's last activity timestamp.

        Args:
            db: Database session
            session_id: Session UUID

        Returns:
            Updated session or None if not found
        """
        session = db.query(self.model).filter(self.model.id == session_id).first()
        if session:
            # 2026-09 修复：原为 ``datetime.utcnow()``——① 在 3.12+ 已废弃；
            # ② 产生 **naive** 值，而本模型 ``updated_at`` 列声明为
            #    ``DateTime(timezone=True)``，属"声明 aware、实际写 naive"的
            #    契约违反。改为带 tzinfo 的 UTC，与列声明一致；写入值语义
            #    （UTC 时刻）不变，故历史数据可比。
            session.updated_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(session)
        return session

    def soft_delete(self, db: Session, *, session_id: str) -> Optional[ChatSession]:
        """
        Soft delete a session (mark as inactive).

        Args:
            db: Database session
            session_id: Session UUID

        Returns:
            Deleted session or None if not found
        """
        session = db.query(self.model).filter(self.model.id == session_id).first()
        if session:
            session.is_active = False
            db.commit()
            db.refresh(session)
        return session

    def restore(self, db: Session, *, session_id: str) -> Optional[ChatSession]:
        """
        Restore a soft-deleted session.

        Args:
            db: Database session
            session_id: Session UUID

        Returns:
            Restored session or None if not found
        """
        session = db.query(self.model).filter(self.model.id == session_id).first()
        if session:
            session.is_active = True
            db.commit()
            db.refresh(session)
        return session

    def count_by_user(
        self, db: Session, *, user_id: str, active_only: bool = True
    ) -> int:
        """
        Count total sessions for a user.

        接口审查批次 2026-09-28：原实现无 ``is_active`` 过滤，与
        ``get_by_user``（默认 ``active_only=True``）口径分叉——软删会话
        计数端点的数字恒 ≥ 列表。现默认对齐列表口径。

        Args:
            db: Database session
            user_id: User identifier
            active_only: Only count active (non-deleted) sessions

        Returns:
            Number of sessions
        """
        query = db.query(self.model).filter(self.model.user_id == user_id)
        if active_only:
            query = query.filter(self.model.is_active == True)  # noqa: E712
        return query.count()


# Create singleton instance
chat_session = CRUDChatSession(ChatSession)
