"""
Database configuration helpers and session utilities.

NOTE: Current CRUD modules use synchronous SQLAlchemy operations.
For production, consider migrating CRUD to async (AsyncSession + await).
The async engine infrastructure is prepared below for future migration.

**engine 是惰性的（§24.2 G10 / §24.3 T1 ⑬）**：模块级 `create_engine` 会让
「只要 import 路径经过本模块就在导入期建池」——缺驱动即 import 失败、
`DATABASE_URL` 拼错的故障落在 import 期而不是启动期的受控校验点，
也堵死「子进程只为跑一个工具」的轻加载路线。`engine` / `SessionLocal`
两个名字仍可直接 import（PEP 562 `__getattr__`），但**首次被真正取用时才建**。
"""

from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Generator, Iterator, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from agentix.config import settings

# R3.1: 知识库（retrieval）域在 PostgreSQL 上的 schema 隔离命名。
# 编排域（My Agentix Core）使用默认 public schema；检索域全部表落在 retrieval schema。
RETRIEVAL_SCHEMA = "retrieval"

_engine: Optional[Engine] = None
_session_factory: Optional[sessionmaker] = None


def _prepare_sqlite(url: str) -> Dict[str, bool]:
    """SQLite 专属：建父目录 + 允许跨线程复用连接。返回 connect_args。"""
    if not url.startswith("sqlite"):
        return {}
    connect_args: Dict[str, bool] = {"check_same_thread": False}
    db_path = url.removeprefix("sqlite:///")
    if db_path not in (":memory:", ""):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return connect_args


def get_engine() -> Engine:
    """统一共享 engine 单例（首次调用才创建）。

    echo 的取值也推迟到这里：`DB_ECHO` 未显式设置时沿用旧的 `DEBUG` 推断
    （G11 口径），且读的是**建池时刻**的配置——比 import 时刻更贴近实际启动配置。
    """
    global _engine
    if _engine is not None:
        return _engine
    echo = bool(settings.DEBUG) if settings.DB_ECHO is None else bool(settings.DB_ECHO)
    _engine = create_engine(
        settings.DATABASE_URL,
        echo=echo,
        future=True,
        pool_pre_ping=True,
        connect_args=_prepare_sqlite(settings.DATABASE_URL),
    )
    return _engine


def get_session_factory() -> sessionmaker:
    """`SessionLocal` 的惰性构造入口（bind 绑到 `get_engine()` 的单例）。"""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(),
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
            class_=Session,
        )
    return _session_factory


def __getattr__(name: str):
    """PEP 562：`engine` / `SessionLocal` 保持可直接 import 的旧形状，但按需建。

    取到后写回 `globals()`，于是同一个对象只建一次，`from ... import engine`
    的既有调用点（含 retrieval 共享 engine 的身份断言）逐位不变。
    """
    if name == "engine":
        resolved = get_engine()
        globals()["engine"] = resolved
        return resolved
    if name == "SessionLocal":
        resolved = get_session_factory()
        globals()["SessionLocal"] = resolved
        return resolved
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    with get_engine().begin() as conn:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {RETRIEVAL_SCHEMA}"))


Base = declarative_base()


def init_db(*, create_all: bool = False) -> None:
    """Initialize the database engine.

    If `create_all` is True the metadata for all models is created.
    """
    if create_all:
        import agentix.infrastructure.persistence.db.models  # noqa: F401
        Base.metadata.create_all(bind=get_engine())


def get_db() -> Generator[Session, None, None]:
    """Provide a database session dependency for FastAPI routes."""
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope(*, commit: bool = True) -> Iterator[Session]:
    """Context manager that handles commit/rollback automatically."""
    session = get_session_factory()()
    try:
        yield session
        if commit:
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
