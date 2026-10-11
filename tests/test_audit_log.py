"""R2.6 高权限操作审计日志单元测试。"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret-key-audit")

from sqlmodel import Session, select  # noqa: E402

from agentix.retrieval.infra.db.session import engine, create_db_and_tables  # noqa: E402
from agentix.retrieval.domain.models.audit import AuditLog  # noqa: E402
from agentix.retrieval.usecases.audit_service import write_audit, _sanitize_details  # noqa: E402
from agentix.retrieval.usecases.tenant_service import TenantService  # noqa: E402
from agentix.retrieval.usecases.kb_service import KbService  # noqa: E402
from agentix.retrieval.usecases.auth_service import AuthService  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _init_db():
    create_db_and_tables()


@pytest.fixture
def session():
    with Session(engine) as s:
        yield s


class TestAuditLogModel:
    def test_audit_log_table_name(self):
        assert AuditLog.__tablename__ == "auditlog"

    def test_audit_log_has_required_fields(self):
        cols = {c.name for c in AuditLog.__table__.columns}
        assert {"id", "actor_user_id", "actor_tenant_id", "action", "target_type", "target_id", "result", "details", "created_at"} <= cols


class TestSanitizeDetails:
    def test_removes_password(self):
        result = _sanitize_details({"name": "test", "password": "secret"})
        assert "password" not in result
        assert "name" in result

    def test_removes_password_hash(self):
        result = _sanitize_details({"password_hash": "abc123"})
        assert "password_hash" not in result

    def test_removes_token(self):
        result = _sanitize_details({"token": "bearer xxx"})
        assert "token" not in result

    def test_removes_api_key(self):
        result = _sanitize_details({"api_key": "key123"})
        assert "api_key" not in result

    def test_none_returns_none(self):
        assert _sanitize_details(None) is None

    def test_empty_dict_returns_none(self):
        assert _sanitize_details({}) is None


class TestWriteAudit:
    def test_write_audit_record(self, session):
        record = write_audit(
            session,
            actor_user_id="user-1",
            actor_tenant_id="tenant-1",
            action="create",
            target_type="kb",
            target_id="kb-1",
            details={"name": "test-kb"},
        )
        session.commit()
        assert record.actor_user_id == "user-1"
        assert record.action == "create"
        assert record.target_type == "kb"
        assert record.target_id == "kb-1"
        assert record.result == "success"

    def test_audit_with_sensitive_data_filtered(self, session):
        record = write_audit(
            session,
            actor_user_id="user-2",
            actor_tenant_id=None,
            action="create",
            target_type="user",
            target_id="user-new",
            details={"email": "test@example.com", "password": "should-be-hidden"},
        )
        session.commit()
        assert "password" not in (record.details or "")
        assert "email" in (record.details or "")


class TestAuditInServices:
    def test_tenant_create_writes_audit(self, session):
        auth = AuthService(session)
        user = auth.register(
            email="r26-t@example.com", username="r26-t", password="password123"
        )
        tenant_svc = TenantService(session)
        tenant_svc.create_tenant(
            name="r26-audit-tenant",
            actor_user_id=user.id,
            actor_tenant_id=user.tenant_id,
        )
        records = list(session.exec(
            select(AuditLog).where(AuditLog.target_type == "tenant")
        ).all())
        assert any(r.target_id == tenant_svc.list_tenants()["items"][0]["id"] for r in records)

    def test_kb_create_writes_audit(self, session):
        auth = AuthService(session)
        user = auth.register(
            email="r26-kb@example.com", username="r26-kb", password="password123"
        )
        kb_svc = KbService(session)
        kb = kb_svc.create_kb(
            name="r26-audit-kb",
            owner_user_id=user.id,
            tenant_id=user.tenant_id,
            actor_user_id=user.id,
            actor_tenant_id=user.tenant_id,
        )
        records = list(session.exec(
            select(AuditLog).where(
                AuditLog.target_type == "kb",
                AuditLog.target_id == kb["id"],
            )
        ).all())
        assert len(records) >= 1
        assert records[0].action == "create"
