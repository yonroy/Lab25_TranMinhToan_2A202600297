from __future__ import annotations

import time
from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError, ProviderResponse


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers."""

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
        cost_budget: float | None = None,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache
        self.cost_budget = cost_budget
        self._cumulative_cost: float = 0.0

    def complete(self, prompt: str) -> GatewayResponse:
        """Return a reliable response or a static fallback."""
        t_start = time.monotonic()

        if self.cache is not None:
            cached, score = self.cache.get(prompt)
            if cached is not None:
                elapsed_ms = (time.monotonic() - t_start) * 1000
                return GatewayResponse(cached, f"cache_hit:{score:.2f}", None, True, elapsed_ms, 0.0)

        # Determine cheapest provider cost for budget routing
        min_cost = min(p.cost_per_1k_tokens for p in self.providers) if self.providers else 0.0

        last_error: str | None = None
        for provider in self.providers:
            # Cost budget check: skip providers more expensive than cheapest when over budget
            if (
                self.cost_budget is not None
                and self._cumulative_cost >= self.cost_budget
                and provider.cost_per_1k_tokens > min_cost
            ):
                last_error = f"cost budget exceeded, skipping {provider.name}"
                continue

            breaker = self.breakers[provider.name]
            try:
                response: ProviderResponse = breaker.call(provider.complete, prompt)
                if self.cache is not None:
                    self.cache.set(prompt, response.text, {"provider": provider.name})
                route = f"primary:{provider.name}" if provider == self.providers[0] else f"fallback:{provider.name}"
                self._cumulative_cost += response.estimated_cost
                elapsed_ms = (time.monotonic() - t_start) * 1000
                return GatewayResponse(
                    text=response.text,
                    route=route,
                    provider=provider.name,
                    cache_hit=False,
                    latency_ms=elapsed_ms,
                    estimated_cost=response.estimated_cost,
                )
            except (ProviderError, CircuitOpenError) as exc:
                last_error = str(exc)
                continue

        elapsed_ms = (time.monotonic() - t_start) * 1000
        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=elapsed_ms,
            estimated_cost=0.0,
            error=last_error,
        )
