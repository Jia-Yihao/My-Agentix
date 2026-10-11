"""
Chat session management API endpoints.

Lightweight user ID system - group sessions by user_id。
逐端点鉴权：``V1_REQUIRE_JWT`` 开启时全部端点要求 Bearer JWT
（见 `v1/deps.py`），默认关保持轻量匿名域。

归属校验（审计批次 A1，2026-09-28）：调用方携带身份声明（JWT sub 或
``X-Device-ID``，见 `v1/deps.py::resolve_v1_caller`）时，跨用户读写他人
会话一律 404（防存在性枚举）；无声明 = 匿名轻量域，行为不变。
"""
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from agentix.infrastructure.core.database import get_db
from agentix.infrastructure.persistence.crud import chat_message, chat_session, chat_session_snapshot
from agentix.interfaces.http.models.chat_message import (
    ChatMessageCreate,
    ChatMessageResponse,
    ChatSessionSnapshotCreate,
    ChatSessionSnapshotResponse,
)
from agentix.interfaces.http.models.chat_session import (
    ChatSessionCreate,
    ChatSessionResponse,
    ChatSessionUpdate,
)
from agentix.interfaces.http.v1.deps import (
    resolve_v1_caller,
    require_v1_user,
    session_visible_to,
)

router = APIRouter(dependencies=[Depends(require_v1_user)])


def _require_visible_session(
    db: Session, session_id: str, caller: Optional[str]
) -> Any:
    """取会话 + 归属校验（审计批次 A1）：不存在或跨用户 → 404。"""
    session = chat_session.get(db, id=session_id)
    if not session or not session_visible_to(
        getattr(session, "user_id", None), caller
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )
    return session


def _require_same_identity(
    user_id: str, caller: Optional[str], *, resource: str
) -> None:
    """列表/计数类「按 user_id 读」的身份一致性（审计批次 A1）。

    持身份者查询他人 user_id → 404；无身份声明保持既有行为。
    """
    if caller is not None and user_id != caller:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{resource} not found"
        )


@router.get("/", response_model=List[ChatSessionResponse])
def get_sessions(
    *,
    db: Session = Depends(get_db),
    user_id: str = Query(..., min_length=1, description="按用户过滤（设备 ID / UUID 等）"),
    # 接口审查批次 2026-09-28：补参数下/上界（原无界——limit=-1 在 SQLite
    # 上等价全量、limit=0 返回空；ops 端点早已是 ge/le 口径，此处对齐）。
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    active_only: bool = True,
    caller: Optional[str] = Depends(resolve_v1_caller),
) -> Any:
    """
    Get list of chat sessions for a specific user.

    ``user_id`` 必填：本组端点无鉴权（轻量 user_id 分组），若允许缺省
    则任何人可无条件枚举**全部用户**的会话标题/时间线 → 数据面泄露。
    必填至少把「读取」收窄到持有目标 user_id 的调用方（安全审查 2026-09-26）。

    鉴权：``V1_REQUIRE_JWT`` 开启时要求 Bearer JWT（默认关，见 `v1/deps.py`）。
    归属（审计批次 A1）：持身份声明查他人 ``user_id`` → 404。
    """
    _require_same_identity(user_id, caller, resource="Sessions")
    sessions = chat_session.get_by_user(
        db,
        user_id=user_id,
        skip=skip,
        limit=limit,
        active_only=active_only
    )

    return sessions


@router.post("/", response_model=ChatSessionResponse, status_code=status.HTTP_201_CREATED)
def create_session(
    *,
    db: Session = Depends(get_db),
    session_in: ChatSessionCreate,
    caller: Optional[str] = Depends(resolve_v1_caller),
) -> Any:
    """
    Create a new chat session.

    Frontend should provide:
    - id: Session UUID
    - user_id: User identifier (device ID, UUID, etc.) - optional
    - title: Session title

    归属（审计批次 A1）：持身份声明时 ``user_id`` 以身份为准（覆盖 body，
    使新会话不可能落在他人名下）。
    """
    payload = session_in.model_dump()
    if caller is not None:
        payload["user_id"] = caller
    session = chat_session.create(db, obj_in=payload)
    return session


@router.get("/{session_id}", response_model=ChatSessionResponse)
def get_session(
    *,
    db: Session = Depends(get_db),
    session_id: str,
    caller: Optional[str] = Depends(resolve_v1_caller),
) -> Any:
    """
    Get a specific chat session by ID.

    归属（审计批次 A1）：跨用户 → 404（防存在性枚举）。
    """
    return _require_visible_session(db, session_id, caller)


@router.patch("/{session_id}", response_model=ChatSessionResponse)
def update_session(
    *,
    db: Session = Depends(get_db),
    session_id: str,
    session_update: ChatSessionUpdate,
    caller: Optional[str] = Depends(resolve_v1_caller),
) -> Any:
    """
    Update a chat session.

    Can update: title, user_id, is_active

    归属（审计批次 A1）：跨用户 → 404；持身份声明时 ``user_id`` 以身份为准。
    """
    session = _require_visible_session(db, session_id, caller)

    if caller is not None and session_update.user_id is not None:
        session_update.user_id = caller

    session = chat_session.update(db, db_obj=session, obj_in=session_update)
    return session


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(
    *,
    db: Session = Depends(get_db),
    session_id: str,
    caller: Optional[str] = Depends(resolve_v1_caller),
) -> None:
    """
    Soft delete a chat session.

    归属（审计批次 A1）：跨用户 → 404。
    """
    _require_visible_session(db, session_id, caller)

    chat_session.soft_delete(db, session_id=session_id)


@router.post(
    "/{session_id}/messages",
    response_model=ChatMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_message_to_session(
    *,
    db: Session = Depends(get_db),
    session_id: str,
    message_in: ChatMessageCreate,
    caller: Optional[str] = Depends(resolve_v1_caller),
) -> Any:
    """
    Add a message to a chat session.

    归属（审计批次 A1）：跨用户写入他人会话 → 404。
    """
    _require_visible_session(db, session_id, caller)

    # Ensure session_id matches（P2-6: 边界处转 Mapping 再覆盖键）
    message_data = message_in.model_dump()
    message_data["session_id"] = session_id

    # Create message
    message = chat_message.create(db, obj_in=message_data)

    # Update session activity
    chat_session.update_activity(db, session_id=session_id)

    return message


@router.post(
    "/{session_id}/snapshot",
    response_model=ChatSessionSnapshotResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_session_snapshot(
    *,
    db: Session = Depends(get_db),
    session_id: str,
    snapshot_in: ChatSessionSnapshotCreate,
    caller: Optional[str] = Depends(resolve_v1_caller),
) -> Any:
    """
    Create a snapshot of a conversation turn for quick restoration.

    归属（审计批次 A1）：跨用户写入他人会话 → 404。
    """
    _require_visible_session(db, session_id, caller)

    # Ensure session_id matches（P2-6: 边界处转 Mapping 再覆盖键）
    snapshot_data = snapshot_in.model_dump()
    snapshot_data["session_id"] = session_id

    # Create snapshot
    snapshot = chat_session_snapshot.create(db, obj_in=snapshot_data)

    return snapshot


@router.get("/user/{user_id}/count", response_model=dict)
def get_user_session_count(
    *,
    db: Session = Depends(get_db),
    user_id: str,
    caller: Optional[str] = Depends(resolve_v1_caller),
) -> Any:
    """
    Get the total number of sessions for a user.

    Useful for analytics and user interface.

    归属（审计批次 A1）：持身份声明查他人 ``user_id`` → 404。
    """
    _require_same_identity(user_id, caller, resource="Sessions")
    count = chat_session.count_by_user(db, user_id=user_id)

    return {
        "user_id": user_id,
        "session_count": count
    }
