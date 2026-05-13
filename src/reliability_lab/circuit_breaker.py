from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, TypeVar

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a circuit is open and calls should fail fast."""


@dataclass(slots=True)
class CircuitBreaker:
    """Production-safe circuit breaker state machine.

    - CLOSED: calls pass through; failure_count increments on each error.
    - OPEN: fail fast (CircuitOpenError) until reset_timeout_seconds elapses.
    - HALF_OPEN: allow one probe; close on success, re-open on failure.
    """

    name: str
    failure_threshold: int
    reset_timeout_seconds: float
    success_threshold: int = 1
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    opened_at: float | None = None
    transition_log: list[dict[str, str | float]] = field(default_factory=list)

    def allow_request(self) -> bool:
        """Return whether a request should be attempted.

        Returns False when OPEN and timeout has not elapsed.
        When timeout elapsed, transition to HALF_OPEN and allow one probe.
        """
        if self.state == CircuitState.OPEN:
            if self.opened_at is not None and time.monotonic() - self.opened_at >= self.reset_timeout_seconds:
                self.failure_count = 0
                self.success_count = 0
                self._transition(CircuitState.HALF_OPEN, "reset_timeout_elapsed")
                return True
            return False
        return True

    def call(self, fn: Callable[..., T], *args: object, **kwargs: object) -> T:
        """Call a function through the circuit breaker."""
        if not self.allow_request():
            raise CircuitOpenError(f"circuit {self.name} is open")
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result

    def record_success(self) -> None:
        """Record success and close from HALF_OPEN if enough probes pass."""
        self.failure_count = 0
        self.success_count += 1
        if self.state == CircuitState.HALF_OPEN and self.success_count >= self.success_threshold:
            self._transition(CircuitState.CLOSED, "probe_success")
            self.success_count = 0

    def record_failure(self) -> None:
        """Record failure and open when threshold is reached."""
        self.success_count = 0
        if self.state == CircuitState.HALF_OPEN:
            # Probe failed — immediately re-open, reset counter for next attempt
            self.failure_count = 0
            self._transition(CircuitState.OPEN, "probe_failure")
            self.opened_at = time.monotonic()
        else:
            self.failure_count += 1
            if self.failure_count >= self.failure_threshold:
                self._transition(CircuitState.OPEN, "failure_threshold")
                self.opened_at = time.monotonic()

    def _transition(self, new_state: CircuitState, reason: str) -> None:
        if self.state == new_state:
            return
        self.transition_log.append(
            {"from": self.state.value, "to": new_state.value, "reason": reason, "ts": time.time()}
        )
        self.state = new_state


class RedisCircuitBreaker:
    """Circuit breaker backed by Redis for shared state across multiple instances.

    State is stored as Redis keys with TTL so it survives process restarts
    and is visible to all gateway instances behind a load balancer.

    Keys used (prefix = "rl:cb:{name}:"):
      failures   — INCR counter, expires after reset_timeout
      state      — string "closed" | "open" | "half_open"
      opened_at  — float Unix timestamp
      transitions — Redis List of JSON-encoded transition dicts
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int,
        reset_timeout_seconds: float,
        redis_url: str,
        success_threshold: int = 1,
    ) -> None:
        import redis as redis_lib

        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout_seconds = reset_timeout_seconds
        self.success_threshold = success_threshold
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)
        self._prefix = f"rl:cb:{name}:"
        # In-memory fallback when Redis is unavailable
        self._fallback = CircuitBreaker(name, failure_threshold, reset_timeout_seconds, success_threshold)

    # ------------------------------------------------------------------
    # Redis helpers — all wrapped in try/except; fallback to in-memory
    # ------------------------------------------------------------------

    def _rget(self, key: str) -> str | None:
        try:
            return self._redis.get(f"{self._prefix}{key}")  # type: ignore[no-any-return]
        except Exception:
            return None

    def _rset(self, key: str, value: str, ex: int | None = None) -> None:
        try:
            self._redis.set(f"{self._prefix}{key}", value, ex=ex)
        except Exception:
            pass

    def _rincr(self, key: str, ex: int) -> int:
        try:
            val = self._redis.incr(f"{self._prefix}{key}")
            self._redis.expire(f"{self._prefix}{key}", ex)
            return int(val)
        except Exception:
            return 0

    def _rdel(self, key: str) -> None:
        try:
            self._redis.delete(f"{self._prefix}{key}")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # State accessors
    # ------------------------------------------------------------------

    @property
    def state(self) -> CircuitState:
        raw = self._rget("state")
        if raw is None:
            return CircuitState.CLOSED
        return CircuitState(raw)

    @property
    def failure_count(self) -> int:
        raw = self._rget("failures")
        return int(raw) if raw else 0

    @property
    def opened_at(self) -> float | None:
        raw = self._rget("opened_at")
        return float(raw) if raw else None

    @property
    def transition_log(self) -> list[dict[str, str | float]]:
        try:
            raw_list = self._redis.lrange(f"{self._prefix}transitions", 0, -1)
            return [json.loads(e) for e in raw_list]
        except Exception:
            return self._fallback.transition_log

    # ------------------------------------------------------------------
    # Public interface — mirrors CircuitBreaker
    # ------------------------------------------------------------------

    def allow_request(self) -> bool:
        state = self.state
        if state == CircuitState.OPEN:
            opened = self.opened_at
            if opened is not None and time.time() - opened >= self.reset_timeout_seconds:
                self._rset("failures", "0")
                self._rset("success_count", "0")
                self._transition(CircuitState.HALF_OPEN, "reset_timeout_elapsed")
                return True
            return False
        return True

    def call(self, fn: Callable[..., T], *args: object, **kwargs: object) -> T:
        if not self.allow_request():
            raise CircuitOpenError(f"circuit {self.name} is open")
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result

    def record_success(self) -> None:
        self._rset("failures", "0")
        sc = self._rincr("success_count", ex=int(self.reset_timeout_seconds) + 5)
        if self.state == CircuitState.HALF_OPEN and sc >= self.success_threshold:
            self._transition(CircuitState.CLOSED, "probe_success")
            self._rset("success_count", "0")

    def record_failure(self) -> None:
        self._rset("success_count", "0")
        if self.state == CircuitState.HALF_OPEN:
            self._rset("failures", "0")
            self._transition(CircuitState.OPEN, "probe_failure")
            self._rset("opened_at", str(time.time()), ex=int(self.reset_timeout_seconds) * 10)
        else:
            fc = self._rincr("failures", ex=int(self.reset_timeout_seconds) * 10)
            if fc >= self.failure_threshold:
                self._transition(CircuitState.OPEN, "failure_threshold")
                self._rset("opened_at", str(time.time()), ex=int(self.reset_timeout_seconds) * 10)

    def _transition(self, new_state: CircuitState, reason: str) -> None:
        current = self.state
        if current == new_state:
            return
        entry = {"from": current.value, "to": new_state.value, "reason": reason, "ts": time.time()}
        try:
            self._redis.rpush(f"{self._prefix}transitions", json.dumps(entry))
        except Exception:
            self._fallback.transition_log.append(entry)
        self._rset("state", new_state.value)

    def reset(self) -> None:
        """Clear all Redis keys for this circuit (useful for tests)."""
        for key in ("state", "failures", "success_count", "opened_at", "transitions"):
            self._rdel(key)
