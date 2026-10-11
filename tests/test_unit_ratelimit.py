"""Unit tests for rate limiter module."""

from agentix.infrastructure.core.ratelimit import (
    RateLimitConfig,
    CircuitBreakerConfig,
    CircuitState,
)


class TestRateLimitConfig:
    """Tests for RateLimitConfig dataclass."""

    def test_default_values(self):
        config = RateLimitConfig()
        assert config.max_tokens == 60
        assert config.refill_rate == 1.0
        assert config.refill_interval == 1.0
        assert config.burst_multiplier == 1.5

    def test_custom_values(self):
        config = RateLimitConfig(
            max_tokens=100,
            refill_rate=5.0,
            refill_interval=2.0,
            burst_multiplier=2.0,
        )
        assert config.max_tokens == 100
        assert config.refill_rate == 5.0


class TestCircuitBreakerConfig:
    """Tests for CircuitBreakerConfig dataclass."""

    def test_default_values(self):
        config = CircuitBreakerConfig()
        assert config.failure_threshold == 5
        assert config.recovery_timeout == 30.0
        assert config.half_open_max_calls == 3

    def test_custom_values(self):
        config = CircuitBreakerConfig(
            failure_threshold=10,
            recovery_timeout=60.0,
            half_open_max_calls=5,
        )
        assert config.failure_threshold == 10


class TestCircuitState:
    """Tests for CircuitState enum."""

    def test_states(self):
        assert CircuitState.CLOSED.value == "closed"
        assert CircuitState.OPEN.value == "open"
        assert CircuitState.HALF_OPEN.value == "half_open"
