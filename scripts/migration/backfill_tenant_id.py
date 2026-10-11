#!/usr/bin/env python
"""存量 tenant_id 回填脚本（R2.7）。

将存量 Tracerag 用户/kb 的 ``owner_user_id → tenant_id`` 回填。
幂等可重跑：已回填的记录跳过，无副作用。

用法::

    python scripts/migration/backfill_tenant_id.py [--dry-run]

环境变量::
    DATABASE_URL          — 数据库连接串（默认 sqlite:///./backend/data/retrieval.db）
    LEGELBOT_DEFAULT_TENANT_NAME — 默认租户名（默认 "default"）
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill_tenant_id")


def main() -> int:
    parser = argparse.ArgumentParser(description="回填存量 User/KB 的 tenant_id")
    parser.add_argument("--dry-run", action="store_true", help="只报告不写入")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent.parent
    sys.path.insert(0, str(project_root))

    from sqlmodel import Session, select

    from agentix.retrieval.infra.db.session import engine, create_db_and_tables
    from agentix.retrieval.domain.models.tenant import Tenant
    from agentix.retrieval.domain.models.user import User
    from agentix.retrieval.domain.models.kb import KnowledgeBase

    create_db_and_tables()

    default_name = os.getenv("LEGELBOT_DEFAULT_TENANT_NAME", "default")

    with Session(engine) as session:
        default_tenant = session.exec(
            select(Tenant).where(Tenant.name == default_name)
        ).first()
        if not default_tenant:
            if args.dry_run:
                logger.info("[dry-run] 会创建默认租户: %s", default_name)
            else:
                default_tenant = Tenant(name=default_name)
                session.add(default_tenant)
                session.commit()
                session.refresh(default_tenant)
                logger.info("创建默认租户: %s (id=%s)", default_name, default_tenant.id)

        if not default_tenant:
            logger.error("默认租户不存在且 --dry-run 模式未创建，退出")
            return 1

        default_id = default_tenant.id

        users_without_tenant = list(
            session.exec(select(User).where(User.tenant_id.is_(None))).all()
        )
        logger.info(
            "待回填用户: %d%s",
            len(users_without_tenant),
            " [dry-run]" if args.dry_run else "",
        )
        if not args.dry_run:
            for u in users_without_tenant:
                u.tenant_id = default_id
                session.add(u)
            if users_without_tenant:
                session.commit()
                logger.info("回填用户 tenant_id: %d", len(users_without_tenant))

        kbs_without_tenant = list(
            session.exec(select(KnowledgeBase).where(KnowledgeBase.tenant_id.is_(None))).all()
        )
        logger.info(
            "待回填知识库: %d%s",
            len(kbs_without_tenant),
            " [dry-run]" if args.dry_run else "",
        )
        if not args.dry_run:
            for kb in kbs_without_tenant:
                owner = session.get(User, kb.owner_user_id)
                kb.tenant_id = owner.tenant_id if owner and owner.tenant_id else default_id
                session.add(kb)
            if kbs_without_tenant:
                session.commit()
                logger.info("回填知识库 tenant_id: %d", len(kbs_without_tenant))

        remaining_users = len(
            list(session.exec(select(User).where(User.tenant_id.is_(None))).all())
        )
        remaining_kbs = len(
            list(session.exec(select(KnowledgeBase).where(KnowledgeBase.tenant_id.is_(None))).all())
        )
        logger.info("回填后剩余: users=%d, kbs=%d", remaining_users, remaining_kbs)

        if remaining_users == 0 and remaining_kbs == 0:
            logger.info("✅ 全部回填完成，tenant_id 无空值")
            return 0
        else:
            logger.warning("⚠️ 仍有空值未回填（可能需要二次运行）")
            return 0 if args.dry_run else 1


if __name__ == "__main__":
    sys.exit(main())
