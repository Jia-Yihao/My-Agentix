"""R2.3 统一 JWT 认证单元测试。"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-jwt-unit-tests")

from sqlmodel import Session  # noqa: E402

from agentix.retrieval.infra.db.session import engine, create_db_and_tables  # noqa: E402
from agentix.retrieval.usecases.auth_service import AuthService  # noqa: E402
from agentix.retrieval.errors import AppError  # noqa: E402
from agentix.security.jwt import (  # noqa: E402
    UserContext,
    create_access_token,
    decode_access_token,
    safe_decode_access_token,
)


@pytest.fixture(scope="module", autouse=True)
def _init_db():
    create_db_and_tables()


@pytest.fixture
def session():
    with Session(engine) as s:
        yield s


class TestJWTToken:
    def test_create_and_decode_token(self):
        token = create_access_token(user_id="u1", tenant_id="t1", role="admin")
        ctx = decode_access_token(token)
        assert ctx.user_id == "u1"
        assert ctx.tenant_id == "t1"
        assert ctx.role == "admin"

    def test_decode_invalid_token_returns_none(self):
        assert safe_decode_access_token("invalid.token.here") is None

    def test_decode_malformed_token_returns_none(self):
        assert safe_decode_access_token("not-a-jwt") is None

    def test_token_contains_tenant_and_role(self):
        token = create_access_token(user_id="u2", tenant_id="t2", role="member")
        ctx = decode_access_token(token)
        assert isinstance(ctx, UserContext)
        assert ctx.tenant_id == "t2"
        assert ctx.role == "member"
        assert ctx.status == "active"

    def test_token_type_mismatch_rejected(self):
        import jwt
        from agentix.config import settings

        payload = {"sub": "u1", "type": "refresh", "tenant_id": None, "role": "member", "status": "active"}
        token = jwt.encode(payload, settings.resolved_secret_key, algorithm="HS256")
        assert safe_decode_access_token(token) is None


class TestBearerExtraction:
    def test_extract_valid_bearer(self):
        from agentix.retrieval.api.deps import _extract_bearer_token

        class MockRequest:
            headers = {"Authorization": "Bearer abc123"}

        assert _extract_bearer_token(MockRequest()) == "abc123"

    def test_no_auth_header(self):
        from agentix.retrieval.api.deps import _extract_bearer_token

        class MockRequest:
            headers = {}

        assert _extract_bearer_token(MockRequest()) is None

    def test_non_bearer_scheme(self):
        from agentix.retrieval.api.deps import _extract_bearer_token

        class MockRequest:
            headers = {"Authorization": "Basic abc123"}

        assert _extract_bearer_token(MockRequest()) is None

    def test_bearer_case_insensitive(self):
        from agentix.retrieval.api.deps import _extract_bearer_token

        class MockRequest:
            headers = {"Authorization": "bearer abc123"}

        assert _extract_bearer_token(MockRequest()) == "abc123"


class TestAuthFlow:
    def test_register_returns_user_with_tenant(self, session):
        auth = AuthService(session)
        user = auth.register(
            email="r23-register@example.com",
            username="r23-register",
            password="password123",
        )
        assert user.tenant_id is not None
        assert user.role == "member"
        assert user.status == "active"

    def test_login_returns_user(self, session):
        auth = AuthService(session)
        auth.register(
            email="r23-login@example.com",
            username="r23-login",
            password="password123",
        )
        user = auth.login(email="r23-login@example.com", password="password123")
        assert user.email == "r23-login@example.com"

    def test_login_wrong_password_rejected(self, session):
        auth = AuthService(session)
        auth.register(
            email="r23-wrong@example.com",
            username="r23-wrong",
            password="password123",
        )
        with pytest.raises(AppError) as exc_info:
            auth.login(email="r23-wrong@example.com", password="wrong-password")
        assert exc_info.value.status_code == 401

    def test_full_jwt_flow(self, session):
        auth = AuthService(session)
        user = auth.register(
            email="r23-flow@example.com",
            username="r23-flow",
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
