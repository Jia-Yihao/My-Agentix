"""R2.1 Tenant 领域模型与 CRUD 单元测试。"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlmodel import Session  # noqa: E402

from agentix.retrieval.infra.db.session import engine, create_db_and_tables  # noqa: E402
from agentix.retrieval.domain.models.tenant import Tenant  # noqa: E402
from agentix.retrieval.usecases.tenant_service import TenantService  # noqa: E402
from agentix.retrieval.errors import AppError  # noqa: E402
from agentix.retrieval.api.response import ErrorCode  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _init_db():
    create_db_and_tables()


@pytest.fixture
def session():
    with Session(engine) as s:
        yield s


@pytest.fixture
def svc(session):
    return TenantService(session)


class TestTenantModel:
    def test_tenant_table_name(self):
        assert Tenant.__tablename__ == "tenant"

    def test_tenant_has_required_fields(self):
        cols = {c.name for c in Tenant.__table__.columns}
        assert {"id", "name", "status", "created_at", "updated_at"} <= cols

    def test_tenant_id_is_primary_key(self):
        pk = [c.name for c in Tenant.__table__.primary_key.columns]
        assert pk == ["id"]


class TestTenantCRUD:
    def test_create_tenant(self, svc):
        result = svc.create_tenant(name="test-create")
        assert result["name"] == "test-create"
        assert result["status"] == "active"
        assert "id" in result

    def test_create_duplicate_name_rejected(self, svc):
        svc.create_tenant(name="dup-name")
        with pytest.raises(AppError) as exc_info:
            svc.create_tenant(name="dup-name")
        assert exc_info.value.error_code == ErrorCode.TENANT_NAME_CONFLICT
        assert exc_info.value.status_code == 409

    def test_get_tenant_by_id(self, svc):
        created = svc.create_tenant(name="test-get")
        got = svc.get_tenant(tenant_id=created["id"])
        assert got is not None
        assert got["name"] == "test-get"

    def test_get_nonexistent_returns_none(self, svc):
        assert svc.get_tenant(tenant_id="nonexistent") is None

    def test_list_tenants(self, svc):
        svc.create_tenant(name="test-list")
        result = svc.list_tenants(page=1, page_size=100)
        assert result["total"] >= 1
        names = [i["name"] for i in result["items"]]
        assert "test-list" in names

    def test_update_tenant_name(self, svc):
        created = svc.create_tenant(name="test-update-before")
        updated = svc.update_tenant(tenant_id=created["id"], name="test-update-after")
        assert updated["name"] == "test-update-after"

    def test_update_tenant_status(self, svc):
        created = svc.create_tenant(name="test-status")
        updated = svc.update_tenant(tenant_id=created["id"], status="disabled")
        assert updated["status"] == "disabled"

    def test_update_tenant_name_conflict(self, svc):
        svc.create_tenant(name="conflict-a")
        b = svc.create_tenant(name="conflict-b")
        with pytest.raises(AppError) as exc_info:
            svc.update_tenant(tenant_id=b["id"], name="conflict-a")
        assert exc_info.value.error_code == ErrorCode.TENANT_NAME_CONFLICT

    def test_update_nonexistent_returns_none(self, svc):
        assert svc.update_tenant(tenant_id="nonexistent", name="x") is None

    def test_delete_tenant(self, svc):
        created = svc.create_tenant(name="test-delete")
        result = svc.delete_tenant(tenant_id=created["id"])
        assert result["status"] == "deleted"
        assert svc.get_tenant(tenant_id=created["id"]) is None

    def test_delete_nonexistent_returns_none(self, svc):
        assert svc.delete_tenant(tenant_id="nonexistent") is None


class TestDefaultTenantSeeding:
    def test_default_tenant_exists_after_migration(self, session):
        from agentix.retrieval.infra.repos.tenant_repo import get_tenant_by_name
        default = get_tenant_by_name(session, "default")
        assert default is not None
        assert default.name == "default"
        assert default.status == "active"
