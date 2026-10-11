"""P0-2 单测：retry.is_retryable / aretry 与 get_llm_client 角色分级。

不依赖任何外部服务（LLM/Redis/DB），可在 CI 离线运行。
对应《My Agentix_架构优化方案》P0-2 验收标准 2/3。
"""

import asyncio

import pytest

from agentix.infrastructure.llm.retry import aretry, is_retryable


class _FakeError(Exception):
    """测试用异常载体，消息文本模拟上游错误。"""


def _err(text: str) -> Exception:
    return _FakeError(text)


class TestIsRetryable:
    @pytest.mark.parametrize(
        "text",
        [
            "AuthenticationError: invalid api key",
            "Error: 401 unauthorized",
            "Error: 403 forbidden",
        ],
    )
    def test_auth_errors_not_retryable(self, text):
        assert is_retryable(_err(text)) is False

    @pytest.mark.parametrize(
        "text",
        [
            "BadRequestError: 400 invalid_request",
            "Error: 400 bad request",
        ],
    )
    def test_param_errors_not_retryable(self, text):
        assert is_retryable(_err(text)) is False

    @pytest.mark.parametrize(
        "text",
        [
            "RateLimitError: 429 rate limit exceeded",
            "APITimeoutError: request timed out",
            "APIConnectionError: connection refused",
            "InternalServerError: 503 temporarily unavailable",
            "Error: 502 bad gateway",
            "Error: service overloaded",
        ],
    )
    def test_transient_errors_retryable(self, text):
        assert is_retryable(_err(text)) is True

    def test_unknown_error_not_retryable(self):
        # 未知错误默认不重试：宁可快速失败，不可盲目放大故障
        assert is_retryable(_err("weird failure")) is False


class TestAretry:
    def test_retries_then_succeeds(self):
        calls = {"n": 0}

        @aretry(max_attempts=3, base_delay=0.001, max_delay=0.002)
        async def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise _err("429 rate limit")
            return "ok"

        assert asyncio.run(flaky()) == "ok"
        assert calls["n"] == 3

    def test_non_retryable_raises_immediately(self):
        calls = {"n": 0}

        @aretry(max_attempts=3, base_delay=0.001, max_delay=0.002)
        async def auth_fail():
            calls["n"] += 1
            raise _err("401 invalid api key")

        with pytest.raises(_FakeError):
            asyncio.run(auth_fail())
        assert calls["n"] == 1  # 401 不重试

    def test_exhausts_attempts_then_raises(self):
        calls = {"n": 0}

        @aretry(max_attempts=3, base_delay=0.001, max_delay=0.002)
        async def always_fail():
            calls["n"] += 1
            raise _err("503 temporarily unavailable")

        with pytest.raises(_FakeError):
            asyncio.run(always_fail())
        assert calls["n"] == 3  # 恰好 max_attempts 次，不无限重试

    def test_cancelled_error_propagates(self):
        @aretry(max_attempts=3, base_delay=0.001, max_delay=0.002)
        async def cancelled():
            raise asyncio.CancelledError()

        # 取消信号必须透传，不可被重试逻辑吞掉
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(cancelled())


class TestGetLlmClientProfiles:
    def test_profiles_have_hard_bounds(self):
        from agentix.application.agents.llm_factory import DEFAULT_PROFILES

        assert set(DEFAULT_PROFILES) >= {
            "router",
            "hallucination",
            "planner",
            "tool",
            "summarizer",
            "general",
        }
        for profile in DEFAULT_PROFILES.values():
            assert profile.timeout > 0, f"{profile.role} 缺少超时硬边界"
            assert profile.max_retries >= 1, f"{profile.role} 缺少重试边界"

    def test_router_profile_is_deterministic_and_fast(self):
        from agentix.application.agents.llm_factory import DEFAULT_PROFILES

        router = DEFAULT_PROFILES["router"]
        assert router.temperature == 0.0
        assert router.timeout == 10.0
        assert router.max_retries == 1

    def test_general_profile_keeps_creativity(self):
        from agentix.application.agents.llm_factory import DEFAULT_PROFILES

        general = DEFAULT_PROFILES["general"]
        assert general.temperature == 0.7
        assert general.timeout == 45.0

    def test_get_llm_client_constructs_with_role_and_legacy_signature(self, monkeypatch):
        """向后兼容：新 role= 签名与老 (temperature, tags) 签名都必须可用。"""
        from agentix.application.agents import llm_factory
        # 注意：agentix.config.settings 导出的直接是 Settings 实例（非模块）
        from agentix.config import settings as settings_instance

        monkeypatch.setattr(settings_instance, "LLM_API_KEY", "test-key", raising=False)

        client_role = llm_factory.get_llm_client(role="router")
        assert client_role is not None

        client_legacy = llm_factory.get_llm_client(0.3, ["legacy-tag"])
        assert client_legacy is not None
