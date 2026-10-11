"""R2.7 存量 tenant_id 回填脚本 + 阶段二端到端验证。"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret-key-phase2-verify")

from sqlmodel import Session, select  # noqa: E402

from agentix.retrieval.infra.db.session import engine, create_db_and_tables  # noqa: E402
from agentix.retrieval.domain.models.tenant import Tenant  # noqa: E402
from agentix.retrieval.domain.models.user import User  # noqa: E402
from agentix.retrieval.domain.models.kb import KnowledgeBase  # noqa: E402
from agentix.retrieval.usecases.auth_service import AuthService  # noqa: E402
from agentix.retrieval.usecases.kb_service import KbService  # noqa: E402
from agentix.retrieval.api.authz import require_kb_in_tenant  # noqa: E402
from agentix.retrieval.errors import AppError  # noqa: E402
from agentix.retrieval.api.response import ErrorCode  # noqa: E402
from agentix.security.jwt import create_access_token, decode_access_token  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _init_db():
    create_db_and_tables()


@pytest.fixture
def session():
    with Session(engine) as s:
        yield s


class TestBackfillIdempotent:
    """回填脚本幂等性验证。"""

    def test_backfill_creates_default_tenant(self, session):
        from agentix.retrieval.infra.repos.tenant_repo import get_tenant_by_name
        default = get_tenant_by_name(session, "default")
        assert default is not None

    def test_backfill_fills_null_tenant_ids(self, session):
        users_null = list(session.exec(select(User).where(User.tenant_id.is_(None))).all())
        kbs_null = list(session.exec(select(KnowledgeBase).where(KnowledgeBase.tenant_id.is_(None))).all())
        assert len(users_null) == 0
        assert len(kbs_null) == 0

    def test_backfill_is_idempotent(self, session):
        from agentix.retrieval.infra.db.migrate import _seed_default_tenant
        _seed_default_tenant()
        _seed_default_tenant()

        defaults = list(session.exec(select(Tenant).where(Tenant.name == "default")).all())
        assert len(defaults) == 1

        users_null = list(session.exec(select(User).where(User.tenant_id.is_(None))).all())
        assert len(users_null) == 0


class TestNoAnonymousAccess:
    """无匿名访客：未带 token 访问知识库接口一律 401（spec §5.5.1-5）。"""

    def test_no_bearer_token_returns_401(self, session):
        from agentix.retrieval.api.deps import get_current_user
        from agentix.retrieval.errors import AppError

        class MockRequest:
            headers = {}
            session = {}

        with pytest.raises(AppError) as exc_info:
            get_current_user(MockRequest(), session)
        assert exc_info.value.status_code == 401
        assert exc_info.value.error_code == ErrorCode.UNAUTHORIZED

    def test_invalid_token_returns_401(self, session):
        from agentix.retrieval.api.deps import get_current_user
        from agentix.retrieval.errors import AppError

        class MockRequest:
            headers = {"Authorization": "Bearer invalid.token.here"}
            session = {}

        with pytest.raises(AppError) as exc_info:
            get_current_user(MockRequest(), session)
        assert exc_info.value.status_code == 401


class TestEndToEndFlow:
    """端到端验证：登录 → 建租户 → 建库 → 跨租户访问验证 403/404。"""

    def test_full_flow(self, session):
        auth = AuthService(session)
        user = auth.register(
            email="r27-e2e@example.com",
            username="r27-e2e",
            password="password123",
        )

        token = create_access_token(
            user_id=user.id,
            tenant_id=user.tenant_id,
            role=user.role,
            status=user.status,
        )
        ctx = decode_access_token(token)
        assert ctx.user_id == user.id
        assert ctx.tenant_id == user.tenant_id

        kb_svc = KbService(session)
        kb = kb_svc.create_kb(
            name="r27-e2e-kb",
            owner_user_id=user.id,
            tenant_id=user.tenant_id,
            actor_user_id=user.id,
            actor_tenant_id=user.tenant_id,
        )
        assert kb["tenant_id"] == user.tenant_id

        accessible = require_kb_in_tenant(session, kb["id"], user.tenant_id)
        assert accessible.name == "r27-e2e-kb"

    def test_cross_tenant_access_blocked(self, session):
        auth = AuthService(session)
        user_a = auth.register(
            email="r27-cross-a@example.com",
            username="r27-cross-a",
            password="password123",
        )

        other_tenant = Tenant(name="r27-other")
        session.add(other_tenant)
        session.commit()
        session.refresh(other_tenant)

        kb_svc = KbService(session)
        kb_other = kb_svc.create_kb(
            name="r27-other-kb",
            owner_user_id=user_a.id,
            tenant_id=other_tenant.id,
        )

        with pytest.raises(AppError) as exc_info:
            require_kb_in_tenant(session, kb_other["id"], user_a.tenant_id)
        assert exc_info.value.status_code == 403
        assert exc_info.value.error_code == ErrorCode.FORBIDDEN

    def test_nonexistent_kb_returns_404(self, session):
        auth = AuthService(session)
        user = auth.register(
            email="r27-404@example.com",
            username="r27-404",
            password="password123",
        )
        with pytest.raises(AppError) as exc_info:
            require_kb_in_tenant(session, "nonexistent", user.tenant_id)
        assert exc_info.value.status_code == 404
        assert exc_info.value.error_code == ErrorCode.KB_NOT_FOUND

    def test_disabled_user_cannot_login(self, session):
        auth = AuthService(session)
        user = auth.register(
            email="r27-disabled@example.com",
            username="r27-disabled",
            password="password123",
        )
        user.status = "disabled"
        session.add(user)
        session.commit()

        with pytest.raises(AppError) as exc_info:
            auth.login(email="r27-disabled@example.com", password="password123")
        assert exc_info.value.status_code == 401


class TestPhase2Summary:
    """阶段二完成度验证。"""

    def test_all_models_have_tenant_id(self):
        user_cols = {c.name for c in User.__table__.columns}
        kb_cols = {c.name for c in KnowledgeBase.__table__.columns}
        assert "tenant_id" in user_cols
        assert "tenant_id" in kb_cols

    def test_jwt_tools_available(self):
        from agentix.security.jwt import create_access_token, decode_access_token, UserContext
        assert create_access_token is not None
        assert decode_access_token is not None
        assert UserContext is not None

    def test_rbac_tools_available(self):
        from agentix.retrieval.api.rbac import require_admin, can_delete, ROLE_ADMIN, ROLE_MEMBER
        assert require_admin is not None
        assert can_delete is not None
        assert ROLE_ADMIN == "admin"
        assert ROLE_MEMBER == "member"

    def test_audit_log_available(self):
        from agentix.retrieval.usecases.audit_service import write_audit
        from agentix.retrieval.domain.models.audit import AuditLog
        assert write_audit is not None
        assert AuditLog is not None

    def test_authz_tenant_functions_available(self):
        from agentix.retrieval.api.authz import (
            require_kb_in_tenant,
            require_doc_in_tenant,
            require_session_in_tenant,
        )
        assert require_kb_in_tenant is not None
        assert require_doc_in_tenant is not None
        assert require_session_in_tenant is not None
