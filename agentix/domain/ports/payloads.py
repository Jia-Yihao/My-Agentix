"""P2-6 (T2) — 持久化 CRUD 载体的领域层 TypedDict 定义。

背景（KNOWN-ISSUE(P2-6) 收口）：此前 ``application``（chat_service）与
``infrastructure``（crud_*）反向依赖 ``interfaces.http.models`` 的 HTTP
pydantic 契约。本模块把这些「创建载体」下沉为 domain 层 stdlib TypedDict：

- ``application`` 构造 TypedDict（或普通 dict）；
- ``infrastructure`` 的 ``CRUDBase.create`` 已放宽为
  ``Union[CreateSchemaType, Mapping[str, Any]]``（T1）；
- ``interfaces`` 的 pydantic 模型只保留 HTTP 契约职责（FastAPI 校验/文档），
  端点在边界处 ``model_dump()`` 后传入（T3）。

设计取舍：
- **纯 stdlib**（typing.TypedDict），符合方案 2.2 依赖规则 3
  （domain 不得依赖 infrastructure/interfaces，且 ports 包零第三方依赖）；
- **不做运行时校验**：TypedDict 是类型标注而非 pydantic 模型，错误键会直接
  在 SQLAlchemy 模型构造处暴露（fail-fast，而非静默忽略）；
- 字段与被替代的 HTTP Create schema 一一镜像，保证边界 ``model_dump()``
  语义等价。

Python 3.10 注意：``NotRequired`` 需 3.11+ 的 typing（或 typing_extensions，
但 ports 包禁第三方依赖），故可选字段一律用 ``Optional[...]`` + 必填键——
调用方显式提供所有键，避免隐式缺省。
"""
from typing import Any, Dict, Optional

from typing import TypedDict


class SessionCreatePayload(TypedDict):
    """镜像 ``interfaces.http.models.chat_session.ChatSessionCreate``。"""

    id: str
    title: str
    user_id: Optional[str]


class MessageCreatePayload(TypedDict):
    """镜像 ``interfaces.http.models.chat_message.ChatMessageCreate``。"""

    session_id: str
    message_type: str
    content: str
    message_metadata: Optional[Dict[str, Any]]
    order_index: int


class SessionSnapshotPayload(TypedDict):
    """镜像 ``interfaces.http.models.chat_message.ChatSessionSnapshotCreate``。"""

    session_id: str
    query: str
    response_data: Dict[str, Any]


class ConversationSnapshotPayload(TypedDict):
    """镜像 ``interfaces.http.models.chat_history.ConversationHistorySnapshotCreate``。"""

    conversation_id: int
    query: str
    response_data: Dict[str, Any]


__all__ = [
    "SessionCreatePayload",
    "MessageCreatePayload",
    "SessionSnapshotPayload",
    "ConversationSnapshotPayload",
]
