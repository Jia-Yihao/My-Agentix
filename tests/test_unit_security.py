"""Unit tests for API key authentication."""

import pytest
from unittest.mock import patch
from fastapi import HTTPException

from agentix.infrastructure.core.security import verify_api_key


class TestVerifyAPIKey:
    """Tests for verify_api_key dependency."""

    @pytest.mark.asyncio
    @patch("agentix.infrastructure.core.security.settings")
    async def test_disabled_auth_returns_empty(self, mock_settings):
        mock_settings.REQUIRE_API_KEY = False
        result = await verify_api_key(x_api_key=None)
        assert result == ""

    @pytest.mark.asyncio
    @patch("agentix.infrastructure.core.security.settings")
    async def test_valid_api_key_accepted(self, mock_settings):
        mock_settings.REQUIRE_API_KEY = True
        mock_settings.API_KEY = "test-secret-key"
        result = await verify_api_key(x_api_key="test-secret-key")
        assert result == "test-secret-key"

    @pytest.mark.asyncio
    @patch("agentix.infrastructure.core.security.settings")
    async def test_invalid_api_key_rejected(self, mock_settings):
        mock_settings.REQUIRE_API_KEY = True
        mock_settings.API_KEY = "test-secret-key"
        with pytest.raises(HTTPException) as exc_info:
            await verify_api_key(x_api_key="wrong-key")
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    @patch("agentix.infrastructure.core.security.settings")
    async def test_missing_api_key_rejected(self, mock_settings):
        mock_settings.REQUIRE_API_KEY = True
        mock_settings.API_KEY = "test-secret-key"
        with pytest.raises(HTTPException) as exc_info:
            await verify_api_key(x_api_key=None)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    @patch("agentix.infrastructure.core.security.settings")
    async def test_empty_api_key_rejected(self, mock_settings):
        mock_settings.REQUIRE_API_KEY = True
        mock_settings.API_KEY = "test-secret-key"
        with pytest.raises(HTTPException) as exc_info:
            await verify_api_key(x_api_key="")
        assert exc_info.value.status_code == 401
