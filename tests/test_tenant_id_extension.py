"""R2.2 User/KnowledgeBase tenant_id 扩展单元测试。"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlmodel import Session  # noqa: E402

from agentix.retrieval.infra.db.session import engine, create_db_and_tables  # noqa: E402
from agentix.retrieval.domain.models.user import User  # noqa: E402
from agentix.retrieval.domain.models.kb import KnowledgeBase  # noqa: E402
from agentix.retrieval.usecases.auth_service import AuthService  # noqa: E402
from agentix.retrieval.usecases.kb_service import KbService  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _init_db():
    create_db_and_tables()


@pytest.fixture
def session():
    with Session(engine) as s:
        yield s


class TestUserTenantExtension:
    def test_user_has_tenant_id_field(self):
        cols = {c.name for c in User.__table__.columns}
        assert "tenant_id" in cols

    def test_user_has_role_field(self):
        cols = {c.name for c in User.__table__.columns}
        assert "role" in cols

    def test_user_has_status_field(self):
        cols = {c.name for c in User.__table__.columns}
        assert "status" in cols

    def test_new_user_gets_default_tenant(self, session):
        auth = AuthService(session)
        user = auth.register(
            email="r22-test@example.com",
            username="r22-test-user",
            password="password123",
        )
        assert user.tenant_id is not None
        assert user.role == "member"
        assert user.status == "active"

    def test_disabled_user_cannot_login(self, session):
        from agentix.retrieval.errors import AppError

        auth = AuthService(session)
        user = auth.register(
            email="r22-disabled@example.com",
            username="r22-disabled-user",
            password="password123",
        )
        user.status = "disabled"
        session.add(user)
        session.commit()

        with pytest.raises(AppError) as exc_info:
            auth.login(email="r22-disabled@example.com", password="password123")
        assert exc_info.value.status_code == 401


class TestKnowledgeBaseTenantExtension:
    def test_kb_has_tenant_id_field(self):
        cols = {c.name for c in KnowledgeBase.__table__.columns}
        assert "tenant_id" in cols

    def test_kb_created_with_tenant_id(self, session):
        auth = AuthService(session)
        user = auth.register(
            email="r22-kb@example.com",
            username="r22-kb-user",
            password="password123",
        )
        kb_svc = KbService(session)
        kb = kb_svc.create_kb(
            name="r22-test-kb",
            description="test",
            owner_user_id=user.id,
            tenant_id=user.tenant_id,
        )
        assert kb["tenant_id"] == user.tenant_id

    def test_list_kbs_filtered_by_tenant(self, session):
        auth = AuthService(session)
        user = auth.register(
            email="r22-list@example.com",
            username="r22-list-user",
            password="password123",
        )
        kb_svc = KbService(session)
        kb_svc.create_kb(
            name="r22-list-kb-1",
            owner_user_id=user.id,
            tenant_id=user.tenant_id,
        )
        result = kb_svc.list_kbs(tenant_id=user.tenant_id, page=1, page_size=100)
        assert result["total"] >= 1
        for item in result["items"]:
            assert item["tenant_id"] == user.tenant_id

    def test_list_kbs_excludes_other_tenants(self, session):
        from agentix.retrieval.domain.models.tenant import Tenant

        other_tenant = Tenant(name="r22-other-tenant")
        session.add(other_tenant)
        session.commit()
        session.refresh(other_tenant)

        auth = AuthService(session)
        user = auth.register(
            email="r22-exclude@example.com",
            username="r22-exclude-user",
            password="password123",
        )
        kb_svc = KbService(session)
        kb_svc.create_kb(
            name="r22-other-tenant-kb",
            owner_user_id=user.id,
            tenant_id=other_tenant.id,
        )
        result = kb_svc.list_kbs(tenant_id=user.tenant_id, page=1, page_size=100)
        names = [i["name"] for i in result["items"]]
        assert "r22-other-tenant-kb" not in names


class TestMigrationBackfill:
    def test_existing_users_backfilled(self, session):
        from agentix.retrieval.infra.repos.tenant_repo import get_tenant_by_name

        default_tenant = get_tenant_by_name(session, "default")
        all_users = list(session.exec(__import__("sqlmodel").select(User)).all())
        for u in all_users:
            if u.email.startswith("r22-"):
                assert u.tenant_id == default_tenant.id
