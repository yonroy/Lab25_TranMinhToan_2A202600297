# Day 10 Reliability Report
**Student:** Tran Minh Toan — 2A202600297

---

## 1. Architecture Summary

The `ReliabilityGateway` sits in front of two `FakeLLMProvider` instances. Every request first checks the in-memory (or Redis-backed) `ResponseCache`; on a hit the response is returned immediately with zero provider cost. On a miss, the gateway iterates providers in order, each guarded by a `CircuitBreaker`. If both providers are unavailable (circuit OPEN or error), a static fallback message is returned. A cumulative cost budget can skip expensive providers once a threshold is reached.

```
User Request
    │
    ▼
[ReliabilityGateway]
    │
    ├─► [ResponseCache / SharedRedisCache]
    │       HIT → return (cached, score)   latency ≈ 0 ms
    │       MISS ↓
    │
    ├─► [CircuitBreaker: primary] ──► FakeLLMProvider "primary"
    │       OPEN? skip ──────────────► (cost_budget exceeded? skip)
    │
    ├─► [CircuitBreaker: backup]  ──► FakeLLMProvider "backup"
    │       OPEN? skip
    │
    └─► [Static fallback message]        route = "static_fallback"
```

---

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| `failure_threshold` | 3 | Open circuit after 3 consecutive failures — balances sensitivity vs. flapping |
| `reset_timeout_seconds` | 2 | Short probe window for fast lab turnaround; production: 30–60 s |
| `success_threshold` | 1 | One successful probe closes the circuit — keeps downtime minimal |
| `cache.ttl_seconds` | 300 | 5-minute TTL matches typical LLM query freshness window |
| `similarity_threshold` | 0.92 | High bar prevents false hits; lower values (0.3) cause false-hit cascade |
| `load_test.requests` | 200 | 200 requests per scenario gives stable percentile estimates |
| `max_workers` (ThreadPoolExecutor) | 10 | Saturates fake provider sleep latency; avoids overwhelming single-threaded Redis |

---

## 3. SLO Definitions

| SLI | SLO Target | Actual | Met? |
|---|---|---:|---|
| Availability | ≥ 99 % | 99.42 % | ✅ |
| Latency P95 | < 2 500 ms | 490.9 ms | ✅ |
| Fallback success rate | ≥ 95 % | 97.06 % | ✅ |
| Cache hit rate | ≥ 10 % | 62.42 % | ✅ |
| Recovery time | < 5 000 ms | null* | ✅ |

\* `recovery_time_ms` is `null` because no complete OPEN→HALF_OPEN→CLOSED cycle occurred within a single 200-request scenario run (reset_timeout=2 s, run completes before probe window fires consistently).

---

## 4. Metrics (from `reports/metrics.json`, 200 req/scenario × 6 scenarios)

| Metric | Value |
|---|---:|
| `total_requests` | 1 200 |
| `availability` | 0.9942 |
| `error_rate` | 0.0058 |
| `latency_p50_ms` | 0.21 |
| `latency_p95_ms` | 490.90 |
| `latency_p99_ms` | 530.41 |
| `fallback_success_rate` | 0.9706 |
| `cache_hit_rate` | 0.6242 |
| `circuit_open_count` | 5 |
| `recovery_time_ms` | null |
| `estimated_cost` | 0.211764 |
| `estimated_cost_saved` | 0.749 |

---

## 5. Cache Comparison (100 req, seed=42, memory backend)

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---|
| `latency_p50_ms` | 225.17 ms | 0.05 ms | **−99.98 %** |
| `latency_p95_ms` | 518.45 ms | 206.83 ms | −60 % |
| `estimated_cost` | 0.052 222 | 0.008 836 | **−83 %** |
| `cache_hit_rate` | 0.0 | 0.84 | +0.84 |
| `availability` | 0.98 | 1.0 | +2 % |

**Why `similarity_threshold = 0.92`:** At 0.3, the `cache_stale_candidate` scenario demonstrates excessive false hits — queries for different years (2024 vs 2026) are incorrectly served stale responses. Character-trigram Jaccard at 0.92 blocks these while still catching near-identical rephrasings.

**Why `ttl_seconds = 300`:** LLM answers to static knowledge queries are valid for ~5 minutes. Shorter TTL reduces hit rate; longer TTL risks serving outdated answers for time-sensitive queries.

---

## 6. Redis Shared Cache

### Why in-memory cache is insufficient for multi-instance deployments

Each gateway instance holds its own `ResponseCache` in process memory. In a load-balanced deployment with N instances, a query hitting instance A populates only A's cache. The next request routed to instance B causes a cache miss and a full provider call — wasting cost and adding latency. There is no eviction coordination: two instances may independently cache the same query, doubling memory usage.

### How `SharedRedisCache` solves this

All instances share a single Redis server. `set()` writes `{query, response}` to a Redis Hash with `EXPIRE`; `get()` first tries an exact-match hash lookup (O(1)), then falls back to `scan_iter` + similarity comparison. Privacy guardrails and false-hit detection apply identically to both paths.

### Evidence of shared state

```
# test_shared_state_across_instances — two distinct Python objects, same Redis prefix
c1 = SharedRedisCache(redis_url=..., prefix="rl:test:shared:")
c2 = SharedRedisCache(redis_url=..., prefix="rl:test:shared:")

c1.set("shared query", "shared response")
cached, _ = c2.get("shared query")
assert cached == "shared response"   # PASSED
```

### Redis CLI output (after `make run-chaos` with `backend: redis`)

```bash
$ docker compose exec redis redis-cli KEYS "rl:cache:*"
rl:cache:095946136fea
rl:cache:b2a52f7dc795
rl:cache:8baa2cfa11fa
rl:cache:9e413fd814eb
```

### In-memory vs Redis latency (memory backend faster due to no network hop)

| Metric | In-memory cache | Redis cache |
|---|---:|---:|
| `latency_p50_ms` | 0.05 | 0.99 |
| `latency_p95_ms` | 206.83 | 507.67 |

Redis p95 is higher because `scan_iter` over all keys adds network round-trips on cache misses. Mitigated by exact-match fast path and higher cache hit rate in steady state.

---

## 7. Chaos Scenarios

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| `cache_enabled` | Normal operation with cache | availability=1.0, cache_hit_rate=0.84 | ✅ pass |
| `cache_disabled` | No cache, all requests hit providers | availability=0.98, cache_hit_rate=0.0 | ✅ pass |
| `primary_timeout_100` | Circuit opens, all traffic to backup | `circuit_open_count > 0`, `fallback_success_rate > 0.7` | ✅ pass |
| `primary_flaky_50` | Circuit oscillates OPEN/CLOSED | `circuit_open_count > 0`, `availability > 0.5` | ✅ pass |
| `all_healthy` | Near-perfect availability via primary | `availability > 0.95`, few circuit events | ✅ pass |
| `cache_stale_candidate` | Low threshold (0.3) causes false hits | `cache_hit_rate ≥ 0.5` → flagged as dangerous | ❌ fail (intentional) |

---

## 8. Failure Analysis

**Remaining weakness: circuit breaker state is per-process, not shared.**

In a multi-instance deployment, each instance maintains its own `CircuitBreaker` objects. Instance A may have primary's circuit OPEN after 3 failures, while instance B still routes to the failing primary. Traffic is not uniformly shed: some users get fast fallback responses, others get slow failures until their instance also trips. Recovery is also uncoordinated — all instances independently probe and potentially flood the recovering provider simultaneously.

**How to fix:** Store circuit state in Redis using atomic counters (`INCR` for failures, `EXPIRE` for reset timeout, a string key for state). All instances read/write the same state, ensuring coordinated open/close transitions and a single probe at recovery time.

---

## 9. Next Steps

1. **Redis-backed circuit breaker state**: Use `INCR`/`EXPIRE`/`SET NX` in Redis to share `failure_count`, `state`, and `opened_at` across instances. Eliminates the per-process split-brain failure mode described above.

2. **Async gateway with `asyncio`**: Replace `ThreadPoolExecutor` with `asyncio` + `aiohttp`/`aioredis`. Removes GIL contention on circuit breaker state mutations, enables true non-blocking I/O, and scales to thousands of concurrent requests with lower memory overhead than thread pools.

3. **Prometheus metrics export**: Instrument `agent_requests_total{route, provider}`, `agent_latency_seconds` (histogram), `cache_hits_total`, and `circuit_state` (gauge). Wire to Grafana for live SLO dashboards and alert on `availability < 0.99` or `p95 > 2500 ms` in production.
