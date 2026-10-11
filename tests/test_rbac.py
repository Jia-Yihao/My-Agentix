"""R2.5 租户内 RBAC（admin / member）单元测试。"""
from __future__ import annotations

import pytest

from agentix.retrieval.api.rbac import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    VALID_ROLES,
    require_admin,
    require_admin_or_member,
    can_delete,
    can_upload,
    can_manage_tenant,
)
from agentix.retrieval.errors import AppError


class TestRoleConstants:
    def test_admin_role_value(self):
        assert ROLE_ADMIN == "admin"

    def test_member_role_value(self):
        assert ROLE_MEMBER == "member"

    def test_valid_roles(self):
        assert VALID_ROLES == {"admin", "member"}


class TestRequireAdmin:
    def test_admin_passes(self):
        require_admin("admin")

    def test_member_blocked(self):
        with pytest.raises(AppError) as exc_info:
            require_admin("member")
        assert exc_info.value.status_code == 403

    def test_none_blocked(self):
        with pytest.raises(AppError) as exc_info:
            require_admin(None)
        assert exc_info.value.status_code == 403

    def test_unknown_role_blocked(self):
        with pytest.raises(AppError) as exc_info:
            require_admin("superadmin")
        assert exc_info.value.status_code == 403


class TestRequireAdminOrMember:
    def test_admin_passes(self):
        require_admin_or_member("admin")

    def test_member_passes(self):
        require_admin_or_member("member")

    def test_guest_blocked(self):
        with pytest.raises(AppError) as exc_info:
            require_admin_or_member("guest")
        assert exc_info.value.status_code == 403

    def test_none_blocked(self):
        with pytest.raises(AppError) as exc_info:
            require_admin_or_member(None)
        assert exc_info.value.status_code == 403


class TestPermissionChecks:
    def test_can_delete_admin(self):
        assert can_delete("admin") is True

    def test_can_delete_member(self):
        assert can_delete("member") is False

    def test_can_upload_admin(self):
        assert can_upload("admin") is True

    def test_can_upload_member(self):
        assert can_upload("member") is True

    def test_can_manage_tenant_admin(self):
        assert can_manage_tenant("admin") is True

    def test_can_manage_tenant_member(self):
        assert can_manage_tenant("member") is False
