"""Prometheus metrics exporter for the reliability gateway.

Exposes the following metrics:
  agent_requests_total{route, provider}  — Counter
  agent_latency_seconds                  — Histogram (buckets: 10ms … 2s)
  cache_hits_total                       — Counter
  circuit_state{name}                    — Gauge (0=closed, 1=open, 2=half_open)

Usage (standalone HTTP server on :8000):
    from reliability_lab.prometheus_exporter import PrometheusExporter
    exporter = PrometheusExporter()
    exporter.start_server(port=8000)   # blocks; call in a thread for background export

Integration with gateway (wrap complete() calls):
    result = gateway.complete(prompt)
    exporter.record(result, gateway)
"""
from __future__ import annotations

from typing import TYPE_CHECKING

try:
    from prometheus_client import (
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        start_http_server,
    )

    _PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PROMETHEUS_AVAILABLE = False

if TYPE_CHECKING:
    from prometheus_client import CollectorRegistry  # noqa: TC002
    from reliability_lab.gateway import GatewayResponse, ReliabilityGateway

from reliability_lab.circuit_breaker import CircuitState

_STATE_TO_INT = {
    CircuitState.CLOSED: 0,
    CircuitState.OPEN: 1,
    CircuitState.HALF_OPEN: 2,
}


class PrometheusExporter:
    """Thin wrapper around prometheus_client metrics.

    If prometheus_client is not installed, all methods are no-ops so the
    rest of the codebase can import and call this class unconditionally.
    """

    def __init__(self, registry: "CollectorRegistry | None" = None) -> None:
        if not _PROMETHEUS_AVAILABLE:
            self._enabled = False
            return
        self._enabled = True
        reg = registry  # explicit variable avoids **kwargs typing issues
        self._requests: Counter = Counter(
            "agent_requests_total",
            "Total requests handled by the gateway",
            ["route", "provider"],
            registry=reg,
        )
        self._latency: Histogram = Histogram(
            "agent_latency_seconds",
            "End-to-end request latency in seconds",
            buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0],
            registry=reg,
        )
        self._cache_hits: Counter = Counter(
            "cache_hits_total",
            "Total cache hits",
            registry=reg,
        )
        self._circuit_state: Gauge = Gauge(
            "circuit_state",
            "Circuit breaker state (0=closed, 1=open, 2=half_open)",
            ["name"],
            registry=reg,
        )

    def record(self, result: "GatewayResponse", gateway: "ReliabilityGateway") -> None:
        """Record a completed gateway response into Prometheus metrics."""
        if not self._enabled:
            return
        provider_label = result.provider or "none"
        route_prefix = result.route.split(":")[0]
        self._requests.labels(route=route_prefix, provider=provider_label).inc()
        self._latency.observe(result.latency_ms / 1000.0)
        if result.cache_hit:
            self._cache_hits.inc()
        for name, breaker in gateway.breakers.items():
            self._circuit_state.labels(name=name).set(_STATE_TO_INT[breaker.state])

    def start_server(self, port: int = 8000) -> None:
        """Start a background HTTP server exposing /metrics on the given port."""
        if not self._enabled:
            raise RuntimeError("prometheus_client is not installed — cannot start metrics server")
        start_http_server(port)
