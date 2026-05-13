from __future__ import annotations

import json
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(
    config: LabConfig,
    provider_overrides: dict[str, float] | None = None,
    cache_similarity_threshold: float | None = None,
    cache_enabled: bool | None = None,
) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }
    enabled = cache_enabled if cache_enabled is not None else config.cache.enabled
    threshold = cache_similarity_threshold if cache_similarity_threshold is not None else config.cache.similarity_threshold
    cache: ResponseCache | SharedRedisCache | None = None
    if enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(config.cache.redis_url, config.cache.ttl_seconds, threshold)
        else:
            cache = ResponseCache(config.cache.ttl_seconds, threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Derive recovery time from circuit breaker transition logs.

    Recovery time = time between circuit opening and next successful close.
    Returns the average recovery time across all breakers, or None if no recovery occurred.
    """
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        open_ts: float | None = None
        for entry in breaker.transition_log:
            if entry["to"] == "open" and open_ts is None:
                open_ts = float(entry["ts"])
            elif entry["to"] == "closed" and open_ts is not None:
                recovery_times.append((float(entry["ts"]) - open_ts) * 1000)
                open_ts = None
    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    """Run a single named chaos scenario."""
    gateway = build_gateway(
        config,
        scenario.provider_overrides or None,
        cache_similarity_threshold=scenario.cache_similarity_threshold,
    )
    metrics = RunMetrics()
    request_count = config.load_test.requests
    lock = threading.Lock()

    def _execute(_: int) -> None:
        prompt = random.choice(queries)
        result = gateway.complete(prompt)
        with lock:
            metrics.total_requests += 1
            metrics.estimated_cost += result.estimated_cost
            if result.cache_hit:
                metrics.cache_hits += 1
                metrics.estimated_cost_saved += 0.001
            if result.route.startswith("fallback:"):
                metrics.fallback_successes += 1
                metrics.successful_requests += 1
            elif result.route == "static_fallback":
                metrics.static_fallbacks += 1
                metrics.failed_requests += 1
            else:
                metrics.successful_requests += 1
            if result.latency_ms:
                metrics.latencies_ms.append(result.latency_ms)

    max_workers = min(10, request_count)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for future in as_completed([executor.submit(_execute, i) for i in range(request_count)]):
            future.result()

    metrics.circuit_open_count = sum(
        1 for breaker in gateway.breakers.values() for t in breaker.transition_log if t["to"] == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    return metrics


def _scenario_passed(name: str, result: RunMetrics) -> bool:
    """Scenario-specific pass/fail criteria derived from transition_log and metrics."""
    if name == "primary_timeout_100":
        # Primary fails 100% — circuit must open AND fallback must handle majority of traffic
        return result.circuit_open_count > 0 and result.fallback_success_rate > 0.7
    if name == "primary_flaky_50":
        # Primary fails 50% — circuit must oscillate (open at least once); availability >50% is enough
        # since this is an intentional degraded-state scenario
        return result.circuit_open_count > 0 and result.availability > 0.5
    if name == "cache_stale_candidate":
        # Low similarity threshold — cache hit rate should be low (few false hits accepted)
        # and system still serves requests with good availability
        return result.cache_hit_rate < 0.5 and result.availability > 0.8
    if name == "all_healthy":
        # Both providers healthy — near-perfect availability expected
        return result.availability > 0.95
    if name == "cache_enabled":
        return result.cache_hit_rate > 0.0 and result.availability > 0.9
    if name == "cache_disabled":
        return result.cache_hit_rate == 0.0 and result.availability > 0.9
    return result.successful_requests > 0


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run all named scenarios from config, or a default run if none defined."""
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {"default": "pass" if metrics.successful_requests > 0 else "fail"}
        return metrics

    # Cache vs no-cache comparison (always runs alongside named scenarios)
    cache_on_scenario = ScenarioConfig(name="cache_enabled", description="cache enabled baseline")
    cache_off_scenario = ScenarioConfig(name="cache_disabled", description="cache disabled baseline")
    cache_on_result = run_scenario(config, queries, cache_on_scenario)
    cache_off_gateway = build_gateway(config, cache_enabled=False)
    cache_off_metrics = RunMetrics()
    for _ in range(config.load_test.requests):
        prompt = random.choice(queries)
        resp = cache_off_gateway.complete(prompt)
        cache_off_metrics.total_requests += 1
        cache_off_metrics.estimated_cost += resp.estimated_cost
        if resp.route.startswith("fallback:"):
            cache_off_metrics.fallback_successes += 1
            cache_off_metrics.successful_requests += 1
        elif resp.route == "static_fallback":
            cache_off_metrics.static_fallbacks += 1
            cache_off_metrics.failed_requests += 1
        else:
            cache_off_metrics.successful_requests += 1
        if resp.latency_ms:
            cache_off_metrics.latencies_ms.append(resp.latency_ms)
    cache_off_metrics.circuit_open_count = sum(
        1 for b in cache_off_gateway.breakers.values() for t in b.transition_log if t["to"] == "open"
    )
    cache_off_metrics.recovery_time_ms = calculate_recovery_time_ms(cache_off_gateway)

    combined = RunMetrics()
    combined.scenarios[cache_on_scenario.name] = "pass" if _scenario_passed(cache_on_scenario.name, cache_on_result) else "fail"
    combined.scenarios[cache_off_scenario.name] = "pass" if _scenario_passed(cache_off_scenario.name, cache_off_metrics) else "fail"
    for m in (cache_on_result, cache_off_metrics):
        combined.total_requests += m.total_requests
        combined.successful_requests += m.successful_requests
        combined.failed_requests += m.failed_requests
        combined.fallback_successes += m.fallback_successes
        combined.static_fallbacks += m.static_fallbacks
        combined.cache_hits += m.cache_hits
        combined.circuit_open_count += m.circuit_open_count
        combined.estimated_cost += m.estimated_cost
        combined.estimated_cost_saved += m.estimated_cost_saved
        combined.latencies_ms.extend(m.latencies_ms)

    for scenario in config.scenarios:
        result = run_scenario(config, queries, scenario)
        combined.scenarios[scenario.name] = "pass" if _scenario_passed(scenario.name, result) else "fail"
        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            if combined.recovery_time_ms is None:
                combined.recovery_time_ms = result.recovery_time_ms
            else:
                combined.recovery_time_ms = (combined.recovery_time_ms + result.recovery_time_ms) / 2

    return combined
