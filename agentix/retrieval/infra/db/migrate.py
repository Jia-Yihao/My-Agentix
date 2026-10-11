"""
增量 schema 迁移：补列、从 folder_path 回填 Folder 树（SQLite / PostgreSQL）
"""
from __future__ import annotations

import logging
import os

from sqlalchemy import inspect, text
from sqlmodel import Session, select

from agentix.retrieval.domain.models.document import Document
from agentix.retrieval.domain.models.tenant import Tenant
from agentix.retrieval.infra.db.session import engine
from agentix.retrieval.infra.repos.folder_repo import get_or_create_folder_by_path

logger = logging.getLogger("agentix.retrieval.migrate")


def _schema() -> str | None:
    """检索域 schema：PG 下为 retrieval（R3.1），SQLite 下为 None。"""
    from agentix.retrieval.infra.db.session import SQLModel

    return SQLModel.metadata.schema


def _qualified(table: str) -> str:
    schema = _schema()
    return f"{schema}.{table}" if schema else table


def _table_exists(inspector, table: str) -> bool:
    return table in inspector.get_table_names(schema=_schema())


def _column_exists(inspector, table: str, column: str) -> bool:
    if not _table_exists(inspector, table):
        return False
    return column in {c["name"] for c in inspector.get_columns(table, schema=_schema())}


def run_migrations() -> None:
    inspector = inspect(engine)

    with engine.begin() as conn:
        if _table_exists(inspector, "document") and not _column_exists(inspector, "document", "folder_id"):
            conn.execute(text(f"ALTER TABLE {_qualified('document')} ADD COLUMN folder_id VARCHAR"))
            logger.info("Added document.folder_id column")

        if _table_exists(inspector, "document") and not _column_exists(inspector, "document", "thumbnail_key"):
            conn.execute(text(f"ALTER TABLE {_qualified('document')} ADD COLUMN thumbnail_key VARCHAR"))
            logger.info("Added document.thumbnail_key column")

        if _table_exists(inspector, "conversation"):
            if not _column_exists(inspector, "conversation", "scope_type"):
                conn.execute(text(f"ALTER TABLE {_qualified('conversation')} ADD COLUMN scope_type VARCHAR"))
                conn.execute(
                    text(f"UPDATE {_qualified('conversation')} SET scope_type = 'kb' WHERE scope_type IS NULL")  # nosec B608 - 表名来自模块常量，无外部输入
                )
                logger.info("Added conversation.scope_type column")
            if not _column_exists(inspector, "conversation", "scope_id"):
                conn.execute(text(f"ALTER TABLE {_qualified('conversation')} ADD COLUMN scope_id VARCHAR"))
                logger.info("Added conversation.scope_id column")

        if _table_exists(inspector, "user") and not _column_exists(inspector, "user", "tenant_id"):
            conn.execute(text(f"ALTER TABLE {_qualified('user')} ADD COLUMN tenant_id VARCHAR"))
            logger.info("Added user.tenant_id column")

        if _table_exists(inspector, "user") and not _column_exists(inspector, "user", "role"):
            conn.execute(text(f"ALTER TABLE {_qualified('user')} ADD COLUMN role VARCHAR DEFAULT 'member'"))
            logger.info("Added user.role column")

        if _table_exists(inspector, "user") and not _column_exists(inspector, "user", "status"):
            conn.execute(text(f"ALTER TABLE {_qualified('user')} ADD COLUMN status VARCHAR DEFAULT 'active'"))
            logger.info("Added user.status column")

        if _table_exists(inspector, "knowledgebase") and not _column_exists(inspector, "knowledgebase", "tenant_id"):
            conn.execute(text(f"ALTER TABLE {_qualified('knowledgebase')} ADD COLUMN tenant_id VARCHAR"))
            logger.info("Added knowledgebase.tenant_id column")

    # 回填：有 folder_path 但无 folder_id 的文档
    with Session(engine) as session:
        docs = list(
            session.exec(
                select(Document).where(
                    Document.folder_path.is_not(None),
                    Document.folder_path != "",
                    Document.folder_id.is_(None),
                )
            ).all()
        )
        changed = 0
        for doc in docs:
            fp = (doc.folder_path or "").strip()
            if not fp:
                continue
            fid = get_or_create_folder_by_path(session, doc.kb_id, fp)
            if fid:
                doc.folder_id = fid
                session.add(doc)
                changed += 1
        if changed:
            session.commit()
            logger.info("Backfilled folder_id for %s documents", changed)

    _seed_default_tenant()


def _seed_default_tenant() -> None:
    """确保存在一个默认租户，并回填存量 User/KB 的 tenant_id（过渡期可空→回填）。"""
    default_name = os.getenv("LEGELBOT_DEFAULT_TENANT_NAME", "default")
    with Session(engine) as session:
        existing = session.exec(select(Tenant).where(Tenant.name == default_name)).first()
        if not existing:
            tenant = Tenant(name=default_name)
            session.add(tenant)
            session.commit()
            session.refresh(tenant)
            logger.info("Seeded default tenant: %s (id=%s)", default_name, tenant.id)
            default_id = tenant.id
        else:
            default_id = existing.id

        from agentix.retrieval.domain.models.user import User
        from agentix.retrieval.domain.models.kb import KnowledgeBase

        users_backfilled = session.exec(
            select(User).where(User.tenant_id.is_(None))
        ).all()
        for u in users_backfilled:
            u.tenant_id = default_id
            session.add(u)
        if users_backfilled:
            session.commit()
            logger.info("Backfilled tenant_id for %s users", len(users_backfilled))

        kbs_backfilled = session.exec(
            select(KnowledgeBase).where(KnowledgeBase.tenant_id.is_(None))
        ).all()
        for kb in kbs_backfilled:
            kb.tenant_id = default_id
            session.add(kb)
        if kbs_backfilled:
            session.commit()
            logger.info("Backfilled tenant_id for %s knowledge bases", len(kbs_backfilled))
