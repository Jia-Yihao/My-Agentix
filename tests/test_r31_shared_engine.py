"""R3.1 统一 PostgreSQL：共享 engine + schema 隔离 的单元验证。

覆盖：
- retrieval 域 engine 与编排域 engine 为同一对象（无双实例）；
- retrieval 兼容层 database_url 与统一 DATABASE_URL 一致（无 retrieval.db 残留）；
- schema 助手在 PG / SQLite 下的取值与 no-op 行为；
- migrate.py 的表名限定在 SQLite 下退化为裸表名。
"""

import pytest  # noqa: E402

from agentix.config import settings as unified_settings  # noqa: E402

# DATABASE_URL 兜底已统一上移至 tests/conftest.py（sqlite:///:memory:），
# 本文件不再自行 setdefault——单文件内 setdefault 会因测试导入顺序失效。


def test_retrieval_engine_is_shared_engine():
    from agentix.infrastructure.core.database import engine as core_engine
    from agentix.retrieval.infra.db.session import engine as retrieval_engine

    assert retrieval_engine is core_engine
    # 兼容入口 get_engine() 也返回同一实例
    from agentix.retrieval.infra.db.session import get_engine

    assert get_engine() is core_engine


def test_retrieval_database_url_matches_unified():
    from agentix.retrieval.settings import settings as retrieval_settings

    assert retrieval_settings.database_url == unified_settings.DATABASE_URL
    # 独立 retrieval.db 默认库已移除
    assert "retrieval.db" not in retrieval_settings.database_url


def test_schema_helpers_sqlite():
    from agentix.infrastructure.core.database import (
        RETRIEVAL_SCHEMA,
        is_postgres,
        retrieval_schema,
    )
    from agentix.retrieval.infra.db.session import SQLModel

    if unified_settings.DATABASE_URL.startswith("sqlite"):
        assert is_postgres() is False
        assert retrieval_schema() is None
        assert SQLModel.metadata.schema is None
    assert RETRIEVAL_SCHEMA == "retrieval"


def test_schema_helpers_postgres(monkeypatch):
    from agentix.infrastructure.core.database import is_postgres, retrieval_schema

    monkeypatch.setattr(unified_settings, "DATABASE_URL", "postgresql://u:p@localhost:5432/agentix")
    assert is_postgres() is True
    assert retrieval_schema() == "retrieval"


def test_ensure_retrieval_schema_noop_on_sqlite():
    """SQLite 下 ensure_retrieval_schema 必须是 no-op 且不抛错。"""
    from agentix.infrastructure.core.database import ensure_retrieval_schema

    if unified_settings.DATABASE_URL.startswith("sqlite"):
        ensure_retrieval_schema()  # 不应连接任何 PG，也不应抛错


def test_qualified_table_name_sqlite_bare():
    from agentix.retrieval.infra.db.migrate import _qualified

    if unified_settings.DATABASE_URL.startswith("sqlite"):
        assert _qualified("document") == "document"


def test_create_db_and_tables_on_shared_engine():
    """共享 engine 上检索域建表 + 增量迁移可完整执行（幂等）。"""
    from agentix.retrieval.infra.db.session import create_db_and_tables

    create_db_and_tables()
    create_db_and_tables()  # 幂等


@pytest.mark.parametrize(
    ("url", "expected_pg"),
    [
        ("sqlite:///./data/x.db", False),
        ("postgresql://u:p@localhost:5432/db", True),
        ("postgres://u:p@localhost:5432/db", True),
        ("mysql+pymysql://u:p@localhost/db", False),
    ],
)
def test_is_postgres_dispatch(url, expected_pg, monkeypatch):
    from agentix.infrastructure.core.database import is_postgres

    monkeypatch.setattr(unified_settings, "DATABASE_URL", url)
    assert is_postgres() is expected_pg
