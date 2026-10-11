"""领域仓储抽象（Protocol）。

依据《My Agentix_架构优化方案》2.2 节依赖规则：
- 规则 2：``application`` 依赖 ``domain.ports`` 抽象，不得直接 new 具体基础设施客户端
- 规则 4：``infrastructure`` 实现 ``domain.ports`` 中定义的 Protocol

本模块**零第三方依赖**（仅 stdlib + typing）——领域层不得依赖任何
infrastructure / interfaces 符号，故这里刻意不 import sqlalchemy / langchain。

类型说明：``db`` 与返回行对象一律用 ``Any`` 表达。领域层不关心「会话是
SQLAlchemy Session 还是别的单位工作」，只关心「仓储能按契约存取」；具体类型
由 infrastructure 侧的实现承担。
"""

from typing import Any, List, Optional, Protocol, runtime_checkable

__all__ = ["SessionRepositoryPort", "MessageRepositoryPort"]


@runtime_checkable
class SessionRepositoryPort(Protocol):
    """会话仓储契约（P1-6 注入点 / P1-7 抽象化）。"""

    def get(self, db: Any, *, id: str) -> Optional[Any]:
        """按 id 取会话；不存在返回 None。"""
        ...

    def create(self, db: Any, *, obj_in: Any) -> Any:
        """创建会话并返回持久化后的实体。"""
        ...

    def update_activity(self, db: Any, *, session_id: str) -> Any:
        """刷新会话活跃时间（用于排序/过期回收）。"""
        ...


@runtime_checkable
class MessageRepositoryPort(Protocol):
    """消息仓储契约（P1-6 注入点 / P1-7 抽象化）。"""

    def get_latest_by_session(self, db: Any, *, session_id: str) -> Optional[Any]:
        """取该会话最后一条消息（order_index 递增的依据）；无则 None。"""
        ...

    def create(self, db: Any, *, obj_in: Any) -> Any:
        """创建消息并返回持久化后的实体。"""
        ...

    def get_by_session(
        self,
        db: Any,
        *,
        session_id: str,
        skip: int = 0,
        limit: int = 100,
    ) -> List[Any]:
        """分页拉取会话历史消息。"""
        ...

    def delete_by_session(self, db: Any, *, session_id: str) -> int:
        """清空会话消息，返回删除条数。"""
        ...
