"""
数据库引擎与 Session 管理（R3.1 统一 PostgreSQL：共享 engine + schema 隔离）

engine 不再由本模块自建，而是复用 agentix.infrastructure.core.database 的
统一 engine（同一 DATABASE_URL 单实例）。隔离方式：
- PostgreSQL：检索域全部表落在 `retrieval` schema（编排域用 public）；
- SQLite（开发/测试）：两侧共享同一文件，schema 隔离为 no-op。
"""
from contextlib import contextmanager
from typing import Iterator

from sqlmodel import SQLModel, Session

from agentix.infrastructure.core.database import (
    ensure_retrieval_schema,
    engine,
    retrieval_schema,
)

# 必须在检索域模型类被导入之前设置：MetaData.schema 只对之后定义的 Table 生效。
# SQLite 下为 None → 使用默认 schema，行为与 R2.7 之前一致。
SQLModel.metadata.schema = retrieval_schema()


def get_engine():
    """兼容入口：返回统一共享 engine 单例（R3.1 起不再自建 engine）。"""
    return engine


def create_db_and_tables():
    # PG：先确保 retrieval schema 存在，再建表（表会带 schema 前缀）
    ensure_retrieval_schema()
    # 确保所有 model 已注册到 metadata
    import agentix.retrieval.domain.models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    from agentix.retrieval.infra.db.migrate import run_migrations

    run_migrations()


@contextmanager
def session_scope() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


def get_session() -> Iterator[Session]:
    """FastAPI Depends 用"""
    with Session(engine) as session:
        yield session
