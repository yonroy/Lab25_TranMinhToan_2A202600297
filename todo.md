# TODO — Lab 25: Reliability Engineering for Production Agents

## Phase 1 — Setup & Orientation (0–30 min) ✅ DONE
- [x] Tạo venv và cài đặt: `pip install -e ".[dev]"` — OK
- [x] Chạy `make test` — 5 passed, 6 skipped (Redis), 1 xfailed
- [x] Chạy `make run-chaos` — baseline lưu tại `reports/metrics_baseline.json`
- [x] Đọc toàn bộ `TODO(student)` trong source files:
  - [x] `circuit_breaker.py` — 3 TODOs: allow_request (line 44), record_success (line 68), record_failure (line 77)
  - [x] `gateway.py` — 2 TODOs: route reasons (line 37), cost budget check (line 38)
  - [x] `cache.py` — 5 TODOs: similarity (line 78), guardrails (line 47), SharedRedisCache class (line 95), get() (line 138), set() (line 154)
  - [x] `chaos.py` — 2 TODOs: cache vs no-cache scenario (line 106), pass/fail criteria (line 119)
- [x] Baseline metrics lưu tại `reports/metrics_baseline.json`

### Baseline metrics (trước khi implement)
```
total_requests:        300
availability:          0.9967
error_rate:            0.0033
latency_p50_ms:        270.19
latency_p95_ms:        309.01
latency_p99_ms:        311.69
fallback_success_rate: 0.8889
cache_hit_rate:        0.9467
circuit_open_count:    1
recovery_time_ms:      null   ← chưa implement
estimated_cost:        0.00665
estimated_cost_saved:  0.284
scenarios:             primary_timeout_100: pass, primary_flaky_50: pass, all_healthy: pass
```

---

## Phase 2 — Circuit Breaker + Fallback (30–75 min) · 25 điểm ✅ DONE

### `circuit_breaker.py`
- [x] `allow_request()`: Reset counters khi OPEN → HALF_OPEN transition
- [x] `record_success()`: Reset failure count, tăng success count; HALF_OPEN → CLOSED khi `success_count >= success_threshold`
- [x] `record_failure()`: Tách 2 nhánh — HALF_OPEN → OPEN với reason `"probe_failure"`; CLOSED → OPEN khi `failure_count >= failure_threshold`

### `gateway.py`
- [x] Route reasons: `"primary:{name}"`, `"fallback:{name}"`, `"cache_hit:{score:.2f}"`
- [x] Full latency timing: `time.monotonic()` bao quanh toàn bộ `complete()`
- [x] Cost budget: `cost_budget` param + `_cumulative_cost` tracker — skip provider đắt khi vượt ngân sách

### Kiểm tra
- [x] `make test` — `test_gateway_contract` pass (6 passed, 1 xfailed)
- [ ] `make run-chaos` — kiểm tra `metrics.json` có `circuit_open_count > 0`
- [x] Viết test bổ sung: `test_circuit_opens_and_backup_serves` — force primary fail 3 lần → circuit OPEN → backup phục vụ

---

## Phase 3 — Metrics + Chaos Scenarios (75–120 min) · 20 điểm ✅ DONE

### `chaos.py`
- [x] Scenario **`primary_timeout_100`**: circuit mở, fallback_success_rate > 0.9 → pass
- [x] Scenario **`primary_flaky_50`**: circuit_open_count > 0 AND availability > 0.8 → pass
- [x] Scenario **`cache_stale_candidate`**: threshold=0.3, cache_hit_rate < 0.5 → fail (cố ý, flag false hits)
- [x] Scenario **`cache_enabled`** / **`cache_disabled`**: cache vs no-cache comparison
- [x] `ScenarioConfig.cache_similarity_threshold` override per-scenario
- [x] `_scenario_passed()`: criteria riêng từng scenario
- [x] `recovery_time_ms` tính từ `transition_log` (không hardcode)

### `metrics.json` — đủ tất cả fields ✅
```
total_requests:        600
availability:          0.9983
error_rate:            0.0017
latency_p50_ms:        0.02
latency_p95_ms:        477.67
latency_p99_ms:        516.58
fallback_success_rate: 0.9787
cache_hit_rate:        0.7917
circuit_open_count:    3         ← circuit đã mở
recovery_time_ms:      null      ← không có chu kỳ đầy đủ OPEN→CLOSED
estimated_cost:        0.06198
estimated_cost_saved:  0.475
scenarios:
  cache_enabled:        pass
  cache_disabled:       pass
  primary_timeout_100:  pass
  primary_flaky_50:     pass
  all_healthy:          pass
  cache_stale_candidate: fail    ← đúng: threshold thấp gây quá nhiều cache hits
```

---

## Phase 4 — In-Memory Cache + Tuning (120–165 min) · 15 điểm ✅ DONE

### `cache.py`
- [x] `similarity()`: exact-match fast path (return 1.0), nâng lên character trigram Jaccard
- [x] `get()`: `_is_uncacheable()` check đầu hàm, `_looks_like_false_hit()` trước khi trả kết quả
- [x] `set()`: `_is_uncacheable()` check — không cache privacy queries
- [x] Fix test: `"refund policy for 2024"` vs `"refund policy for 2026"` → `_looks_like_false_hit()` trả `True` → không match

### Kiểm tra
- [x] `test_todo_requirements.py::test_semantic_cache_should_not_false_hit_different_intent` → **PASSED** (xfail marker đã xóa)
- [x] 7/7 tests passed (không còn xfailed)

### Báo cáo so sánh cache on vs off (100 requests, seed=42)
| metric          | cache ON  | cache OFF |
|-----------------|-----------|-----------|
| latency_p50_ms  | 0.05      | 225.17    |
| latency_p95_ms  | 206.83    | 518.45    |
| cost            | 0.008836  | 0.052222  |
| cost_saved      | 0.084     | 0.0       |
| cache_hit_rate  | 0.84      | 0.0       |
| availability    | 1.0       | 0.98      |

**Lý do chọn `similarity_threshold=0.92` và `ttl_seconds=300`:**
- Threshold 0.92 đủ chặt để ngăn false hits (thấp hơn → false positives như cache_stale_candidate scenario)
- TTL 300s (5 phút) phù hợp với queries không thay đổi nhanh; ngắn hơn thì cache_hit_rate giảm

---

## Phase 5 — Redis Shared Cache (165–210 min) · 15 điểm

### Setup
- [ ] Chạy `make docker-up` → Redis trên `localhost:6379`

### `cache.py` → `SharedRedisCache`
- [ ] Implement `set()`: lưu query/response vào Redis Hash với TTL (`EXPIRE`)
- [ ] Implement `get()`:
  - [ ] Exact match: hash query → `HGET` trực tiếp
  - [ ] Similarity scan: `scan_iter(prefix*)`, so sánh similarity từng entry
  - [ ] Áp dụng privacy guardrails và false-hit detection
- [ ] Xử lý Redis down gracefully (catch `ConnectionError`, fallback)

### Kiểm tra Redis
- [ ] `make test` → tất cả 6 test trong `test_redis_cache.py` pass:
  - [ ] Connection works
  - [ ] Set + exact get trả về đúng giá trị (score 1.0)
  - [ ] TTL expiry (entry mất sau TTL)
  - [ ] Shared state: 2 instance `SharedRedisCache` thấy cùng data
  - [ ] Privacy queries bỏ qua cache
  - [ ] False-hit detection với queries khác năm
- [ ] Chuyển config sang `backend: redis` → `make run-chaos`
- [ ] Chạy `docker compose exec redis redis-cli KEYS "rl:cache:*"` → thấy keys

---

## Phase 6 — Load Test + Final Report (210–240 min) · 15 điểm

### Load Test
- [ ] Tăng `load_test.requests` lên 200+ trong config
- [ ] (Stretch) Thêm concurrency với `ThreadPoolExecutor`
- [ ] Chạy dưới nhiều config khác nhau, ghi kết quả

### `reports/final_report.md`
- [ ] Copy từ `reports/report_template.md`
- [ ] **Architecture summary**: 2–3 câu + sơ đồ text: `User → Gateway → [Cache] → [Circuit Breaker] → Provider A / B → [Static Fallback]`
- [ ] **Configuration table**: mỗi param + giá trị + lý do
- [ ] **Metrics table**: paste từ `metrics.json` (đủ tất cả fields)
- [ ] **Chaos scenario table**: mỗi scenario — expected vs. observed — pass/fail
- [ ] **Cache comparison**: bảng with/without cache
- [ ] **Redis shared cache**: giải thích tại sao cần, bằng chứng 2 instance dùng chung, Redis CLI output
- [ ] **Failure analysis**: 1 điểm yếu còn lại + cách khắc phục
- [ ] **Next steps**: 2–3 cải tiến cụ thể

---

## Code Quality (trước khi nộp)
- [ ] `make typecheck` — type hints đầy đủ trong tất cả code đã viết
- [ ] `make lint` — không lỗi lint
- [ ] `make test` — 0 failures (xfail OK)
- [ ] `make run-chaos` — `metrics.json` tái tạo được

---

## Deliverables cần nộp
- [ ] Source code — tất cả TODOs hoàn thành trong `src/reliability_lab/`
- [ ] `reports/metrics.json` — generated bởi `make run-chaos`
- [ ] `reports/final_report.md` — đủ tất cả sections
- [ ] Screenshot / log của `make test` passing (có Redis)
- [ ] `docker-compose.yml` — đã có sẵn

---

## Stretch Goals (extra credit)
- [ ] Concurrency: `ThreadPoolExecutor` trong `run_simulation`
- [ ] Redis-backed circuit state (INCR/EXPIRE trong Redis)
- [ ] Redis graceful degradation → fallback sang in-memory cache
- [ ] False-hit analysis: log mọi cache hit với similarity score
- [ ] Cost-aware routing: khi budget đạt 80% → route sang model rẻ hơn
- [ ] Property-based tests với `hypothesis` cho circuit breaker
- [ ] Prometheus export: `agent_requests_total`, `agent_latency_seconds`, `cache_hits_total`, `circuit_state`
- [ ] SLO definition: availability >= 99%, P95 < 2.5s → bảng pass/fail
