"""
Database configuration helpers and session utilities.

NOTE: Current CRUD modules use synchronous SQLAlchemy operations.
For production, consider migrating CRUD to async (AsyncSession + await).
The async engine infrastructure is prepared below for future migration.
"""

from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Generator, Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from agentix.config import settings

_DEFAULT_LOG_ECHO = bool(settings.DEBUG)

# R3.1: 知识库（retrieval）域在 PostgreSQL 上的 schema 隔离命名。
# 编排域（My Agentix Core）使用默认 public schema；检索域全部表落在 retrieval schema。
RETRIEVAL_SCHEMA = "retrieval"

_connect_args: Dict[str, bool] = {}
if settings.DATABASE_URL.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}
    _db_path = settings.DATABASE_URL.removeprefix("sqlite:///")
    if _db_path not in (":memory:", ""):
        Path(_db_path).parent.mkdir(parents=True, exist_ok=True)

# Sync engine (used by all CRUD operations)
engine = create_engine(
    settings.DATABASE_URL,
    echo=_DEFAULT_LOG_ECHO,
    future=True,
    pool_pre_ping=True,
    connect_args=_connect_args,
)


def is_postgres() -> bool:
    """当前 DATABASE_URL 是否为 PostgreSQL（schema 隔离仅 PG 生效）。"""
    return settings.DATABASE_URL.startswith(("postgresql", "postgres"))


def retrieval_schema() -> str | None:
    """检索域应使用的 schema 名：PG 下为 retrieval，SQLite 下为 None（默认 schema）。"""
    return RETRIEVAL_SCHEMA if is_postgres() else None


def ensure_retrieval_schema() -> None:
    """在 PostgreSQL 上确保 retrieval schema 存在（幂等）。

    SQLite 下为 no-op（SQLite 不支持 schema 隔离，两侧共享同一文件）。
    必须在检索域 create_all 之前调用。
    """
    if not is_postgres():
        return
    with engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {RETRIEVAL_SCHEMA}"))

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    class_=Session,
)

Base = declarative_base()


def init_db(*, create_all: bool = False) -> None:
    """Initialize the database engine.

    If `create_all` is True the metadata for all models is created.
    """
    if create_all:
        import agentix.infrastructure.persistence.db.models  # noqa: F401
        Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    """Provide a database session dependency for FastAPI routes."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope(*, commit: bool = True) -> Iterator[Session]:
    """Context manager that handles commit/rollback automatically."""
    session = SessionLocal()
    try:
        yield session
        if commit:
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
