from reliability_lab.cache import ResponseCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitState
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.providers import FakeLLMProvider


def test_gateway_returns_response_with_route_reason() -> None:
    provider = FakeLLMProvider("primary", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.001)
    breaker = CircuitBreaker("primary", failure_threshold=2, reset_timeout_seconds=1)
    gateway = ReliabilityGateway([provider], {"primary": breaker}, ResponseCache(60, 0.5))
    result = gateway.complete("hello world")
    assert result.text
    assert result.route.startswith(("primary:", "fallback:", "cache_hit:", "static_fallback"))


def test_circuit_opens_and_backup_serves() -> None:
    """Force primary to fail N times → circuit opens → backup provider serves."""
    primary = FakeLLMProvider("primary", fail_rate=1.0, base_latency_ms=1, cost_per_1k_tokens=0.01)
    backup = FakeLLMProvider("backup", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.006)
    breaker_primary = CircuitBreaker("primary", failure_threshold=3, reset_timeout_seconds=60)
    breaker_backup = CircuitBreaker("backup", failure_threshold=3, reset_timeout_seconds=60)
    gateway = ReliabilityGateway(
        [primary, backup],
        {"primary": breaker_primary, "backup": breaker_backup},
    )

    # First 3 calls hit primary (fail), circuit opens on the 3rd failure
    for _ in range(3):
        result = gateway.complete("test query")
        # Should be served by backup since primary fails
        assert result.route.startswith("fallback:backup") or result.route == "static_fallback"

    # Circuit must be open now
    assert breaker_primary.state == CircuitState.OPEN

    # Subsequent calls skip primary entirely and go straight to backup
    result = gateway.complete("another query")
    assert result.route.startswith("fallback:backup")
    assert result.provider == "backup"
