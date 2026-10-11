"""Unit tests for the LLM factory."""

import pytest
from unittest.mock import patch, MagicMock
from agentix.application.agents.llm_factory import get_llm_client, DEFAULT_PROFILES


class TestLLMFactory:
    """Tests for get_llm_client factory function."""

    @patch("agentix.application.agents.llm_factory.settings")
    def test_raises_without_api_key(self, mock_settings):
        mock_settings.OPENAI_API_KEY = None
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY is not configured"):
            get_llm_client()

    @patch("agentix.application.agents.llm_factory.settings")
    @patch("agentix.application.agents.llm_factory._TrackedChatOpenAI")
    def test_creates_client_with_default_params(self, mock_chat, mock_settings):
        mock_settings.OPENAI_API_KEY = "test-key"
        mock_settings.OPENAI_API_BASE = "http://test.com"

        get_llm_client()

        # P0-2 role-based profiles: default role is "general"
        # (model/temperature come from DEFAULT_PROFILES, fixed at import time)
        # P0-4b: get_llm_client constructs _TrackedChatOpenAI (with role_tag)
        mock_chat.assert_called_once_with(
            openai_api_key="test-key",
            model_name=DEFAULT_PROFILES["general"].model,
            openai_api_base="http://test.com",
            temperature=0.7,
            max_tokens=None,
            timeout=45.0,
            max_retries=2,
            tags=["general"],
            role_tag="general",
        )

    @patch("agentix.application.agents.llm_factory.settings")
    @patch("agentix.application.agents.llm_factory._TrackedChatOpenAI")
    def test_creates_client_with_custom_params(self, mock_chat, mock_settings):
        mock_settings.OPENAI_API_KEY = "test-key"
        mock_settings.OPENAI_API_BASE = "http://test.com"

        get_llm_client(temperature=0.3, tags=["router", "test"])

        mock_chat.assert_called_once_with(
            openai_api_key="test-key",
            model_name=DEFAULT_PROFILES["general"].model,
            openai_api_base="http://test.com",
            temperature=0.3,
            max_tokens=None,
            timeout=45.0,
            max_retries=2,
            tags=["router", "test"],
            role_tag="general",
        )

    @patch("agentix.application.agents.llm_factory.settings")
    @patch("agentix.application.agents.llm_factory.ChatOpenAI")
    def test_returns_chat_openai_instance(self, mock_chat, mock_settings):
        mock_settings.OPENAI_API_KEY = "test-key"
        mock_settings.OPENAI_MODEL = "test-model"
        mock_settings.OPENAI_API_BASE = "http://test.com"
        mock_chat.return_value = MagicMock()

        client = get_llm_client()
        assert client is not None
