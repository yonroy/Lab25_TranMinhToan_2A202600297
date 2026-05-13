"""Property-based tests for CircuitBreaker using Hypothesis.

These tests verify state-machine invariants by generating random sequences
of success/failure events and asserting structural properties that must
always hold — regardless of the specific sequence.
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from reliability_lab.circuit_breaker import CircuitBreaker, CircuitState


def _make_breaker(failure_threshold: int = 3, reset_timeout: float = 60.0) -> CircuitBreaker:
    return CircuitBreaker(
        name="test",
        failure_threshold=failure_threshold,
        reset_timeout_seconds=reset_timeout,
        success_threshold=1,
    )


@given(st.integers(min_value=1, max_value=5), st.integers(min_value=1, max_value=10))
def test_open_circuit_rejects_calls_without_recording_failure(threshold: int, extra_calls: int) -> None:
    """Once circuit opens, call() raises CircuitOpenError immediately (no record_failure side-effects)."""
    cb = _make_breaker(failure_threshold=threshold)
    for _ in range(threshold):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN
    log_len_before = len(cb.transition_log)
    for _ in range(extra_calls):
        try:
            cb.call(lambda: None)
        except Exception:
            pass
    # No new transitions triggered by the rejected calls
    assert len(cb.transition_log) == log_len_before
    assert cb.state == CircuitState.OPEN


@given(st.integers(min_value=1, max_value=8))
def test_circuit_opens_exactly_at_threshold(threshold: int) -> None:
    """Circuit must remain CLOSED until exactly `failure_threshold` consecutive failures."""
    cb = _make_breaker(failure_threshold=threshold)
    for i in range(threshold - 1):
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED, f"opened early at failure {i + 1}"
    cb.record_failure()
    assert cb.state == CircuitState.OPEN


@given(st.integers(min_value=1, max_value=6))
def test_success_resets_failure_count_in_closed(n_failures: int) -> None:
    """A single success in CLOSED state must reset the failure counter."""
    cb = _make_breaker(failure_threshold=n_failures + 1)  # won't open
    for _ in range(n_failures):
        cb.record_failure()
    assert cb.failure_count == n_failures
    cb.record_success()
    assert cb.failure_count == 0
    assert cb.state == CircuitState.CLOSED


@given(st.lists(st.booleans(), min_size=1, max_size=30))
@settings(max_examples=200)
def test_state_machine_valid_transitions(events: list[bool]) -> None:
    """State must always be one of the three valid states; counters never negative."""
    cb = _make_breaker(failure_threshold=3, reset_timeout=60.0)
    valid_states = {CircuitState.CLOSED, CircuitState.OPEN, CircuitState.HALF_OPEN}
    for success in events:
        if success:
            cb.record_success()
        else:
            cb.record_failure()
        assert cb.state in valid_states
        assert cb.failure_count >= 0
        assert cb.success_count >= 0


@given(st.integers(min_value=1, max_value=5))
def test_transition_log_records_every_state_change(threshold: int) -> None:
    """Every state change must produce exactly one entry in transition_log."""
    cb = _make_breaker(failure_threshold=threshold)
    for _ in range(threshold):
        cb.record_failure()
    # CLOSED → OPEN: 1 transition
    assert len(cb.transition_log) == 1
    assert cb.transition_log[0]["from"] == CircuitState.CLOSED.value
    assert cb.transition_log[0]["to"] == CircuitState.OPEN.value


@given(st.integers(min_value=1, max_value=5), st.integers(min_value=1, max_value=5))
def test_alternating_failure_success_does_not_corrupt_state(threshold: int, cycles: int) -> None:
    """Alternating single failure / success in CLOSED must never open the circuit."""
    cb = _make_breaker(failure_threshold=threshold + 1)
    for _ in range(cycles):
        cb.record_failure()
        cb.record_success()
    # failure_count resets on every success — circuit must stay CLOSED
    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 0
