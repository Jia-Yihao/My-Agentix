"""R2.4 逐层归属校验（authz.py 升级至租户层）单元测试。"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret-key-authz")

from sqlmodel import Session  # noqa: E402

from agentix.retrieval.infra.db.session import engine, create_db_and_tables  # noqa: E402
from agentix.retrieval.domain.models.tenant import Tenant  # noqa: E402
from agentix.retrieval.usecases.auth_service import AuthService  # noqa: E402
from agentix.retrieval.usecases.kb_service import KbService  # noqa: E402
from agentix.retrieval.api.authz import (  # noqa: E402
    require_kb_in_tenant,
    require_kb_owned,
    require_doc_in_tenant,
    require_session_in_tenant,
)
from agentix.retrieval.errors import AppError  # noqa: E402
from agentix.retrieval.api.response import ErrorCode  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _init_db():
    create_db_and_tables()


@pytest.fixture(scope="module")
def session():
    with Session(engine) as s:
        yield s


@pytest.fixture(scope="module")
def setup_data(session):
    auth = AuthService(session)
    user_a = auth.register(
        email="r24-a@example.com", username="r24-a", password="password123"
    )
    other_tenant = Tenant(name="r24-other-tenant")
    session.add(other_tenant)
    session.commit()
    session.refresh(other_tenant)

    kb_svc = KbService(session)
    kb_a = kb_svc.create_kb(
        name="r24-kb-a", owner_user_id=user_a.id, tenant_id=user_a.tenant_id
    )
    kb_other = kb_svc.create_kb(
        name="r24-kb-other", owner_user_id=user_a.id, tenant_id=other_tenant.id
    )
    return {
        "user_a": user_a,
        "other_tenant": other_tenant,
        "kb_a_id": kb_a["id"],
        "kb_other_id": kb_other["id"],
    }


class TestRequireKbInTenant:
    def test_same_tenant_access_ok(self, session, setup_data):
        kb = require_kb_in_tenant(
            session, setup_data["kb_a_id"], setup_data["user_a"].tenant_id
        )
        assert kb.name == "r24-kb-a"

    def test_cross_tenant_blocked_403(self, session, setup_data):
        with pytest.raises(AppError) as exc_info:
            require_kb_in_tenant(
                session, setup_data["kb_other_id"], setup_data["user_a"].tenant_id
            )
        assert exc_info.value.error_code == ErrorCode.FORBIDDEN
        assert exc_info.value.status_code == 403

    def test_nonexistent_kb_404(self, session, setup_data):
        with pytest.raises(AppError) as exc_info:
            require_kb_in_tenant(
                session, "nonexistent-kb-id", setup_data["user_a"].tenant_id
            )
        assert exc_info.value.error_code == ErrorCode.KB_NOT_FOUND
        assert exc_info.value.status_code == 404


class TestCompatWrapper:
    def test_compat_require_kb_owned_same_user(self, session, setup_data):
        kb = require_kb_owned(
            session, setup_data["kb_a_id"], setup_data["user_a"].id
        )
        assert kb.name == "r24-kb-a"

    def test_compat_require_kb_owned_cross_tenant(self, session, setup_data):
        with pytest.raises(AppError) as exc_info:
            require_kb_owned(
                session, setup_data["kb_other_id"], setup_data["user_a"].id
            )
        assert exc_info.value.error_code == ErrorCode.FORBIDDEN


class TestRequireDocInTenant:
    def test_nonexistent_doc_404(self, session, setup_data):
        with pytest.raises(AppError) as exc_info:
            require_doc_in_tenant(
                session, "nonexistent-doc-id", setup_data["user_a"].tenant_id
            )
        assert exc_info.value.error_code == ErrorCode.DOCUMENT_NOT_FOUND


class TestRequireSessionInTenant:
    def test_nonexistent_session_404(self, session, setup_data):
        with pytest.raises(AppError) as exc_info:
            require_session_in_tenant(
                session, "nonexistent-session-id", setup_data["user_a"].tenant_id
            )
        assert exc_info.value.error_code == ErrorCode.CONVERSATION_NOT_FOUND


class TestNoCrossTenantLeak:
    def test_404_and_403_distinct(self, session, setup_data):
        from agentix.retrieval.api.response import get_status_code

        code_404 = get_status_code(ErrorCode.KB_NOT_FOUND)
        code_403 = get_status_code(ErrorCode.FORBIDDEN)
        assert code_404 == 404
        assert code_403 == 403
        assert code_404 != code_403
