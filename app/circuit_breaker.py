"""
Per-(logical model) circuit breaker: closed/open/half-open state machine
that protects a struggling or fully-down provider from continued load
during an outage, and protects the gateway from spending retry budget and
latency on a candidate that keeps failing across many separate requests.

This reacts to REAL request outcomes - one signal per candidate-attempt,
recorded by app.fallback.dispatch_with_fallback after that candidate's own
retry budget (Step 11) is exhausted or it succeeds - not the passive
background probes from Step 9's HealthTracker. The two are complementary,
different signal sources answering different questions:
  - Step 9 asks "can we reach this model at all, right now, via a cheap
    side-channel probe", on a fixed interval, independent of live traffic.
  - This asks "has actual traffic to this model been failing lately",
    driven only by real requests as they happen.
They're deliberately not merged into one component - keeping them separate
means neither has to worry about the other's timing or side effects, and
it's easy to reason about which one is responsible for what in an outage.

States:
  - CLOSED: normal operation. Consecutive failures are counted; hitting
    failure_threshold trips to OPEN.
  - OPEN: every check is short-circuited (CircuitOpenError, no provider
    call attempted) until cooldown_seconds has elapsed since opening -
    this IS the "provider protection during outages" requirement: once
    tripped, no more traffic is sent to that model until the cooldown
    passes, however many requests arrive in the meantime.
  - HALF_OPEN: after cooldown, up to half_open_max_trials trial requests
    are allowed through to test recovery. A trial success closes the
    circuit (failure count reset); a trial failure reopens it (cooldown
    restarts from now). Requests arriving once the trial slots are full
    are short-circuited exactly like OPEN.

Explicitly NOT in scope: cross-instance shared circuit state. This is
in-memory per-process, same reasoning as Step 9's health tracker - a
breaker tripped by this instance's traffic shouldn't silently affect
another instance's view of the same provider.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum

from app.config import CircuitBreakerConfig
from app.metrics import record_circuit_breaker_transition

logger = logging.getLogger("llm_gateway.circuit_breaker")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class _BreakerEntry:
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: float | None = None
    half_open_trials_in_flight: int = 0
    last_transition_at: float = field(default_factory=time.time)


class CircuitOpenError(Exception):
    """Raised by check_and_reserve() when a candidate is short-circuited -
    either OPEN and still cooling down, or HALF_OPEN with no trial slot
    free right now."""

    def __init__(self, key: str, state: CircuitState, retry_after: float | None):
        self.key = key
        self.state = state
        self.retry_after = retry_after
        super().__init__(f"circuit '{key}' is {state.value}, rejecting without a provider call")


class CircuitBreaker:
    def __init__(self, config: CircuitBreakerConfig):
        self._config = config
        self._entries: dict[str, _BreakerEntry] = {}

    def _entry(self, key: str) -> _BreakerEntry:
        return self._entries.setdefault(key, _BreakerEntry())

    def _transition(
        self, key: str, entry: _BreakerEntry, new_state: CircuitState, reason: str
    ) -> None:
        old_state = entry.state
        entry.state = new_state
        entry.last_transition_at = time.time()
        logger.warning(
            "circuit_breaker_transition key=%s %s -> %s reason=%s",
            key, old_state.value, new_state.value, reason,
        )
        record_circuit_breaker_transition(key, old_state.value, new_state.value)

    def check_and_reserve(self, key: str) -> None:
        """Call before attempting a candidate. Raises CircuitOpenError if it
        should be short-circuited right now. If the circuit is HALF_OPEN and
        a trial slot is free, reserves one - the caller MUST then call
        record_outcome() for this key, success or not, to release it."""
        entry = self._entry(key)
        now = time.time()

        if entry.state == CircuitState.OPEN:
            elapsed = now - (entry.opened_at or now)
            if elapsed >= self._config.cooldown_seconds:
                self._transition(
                    key, entry, CircuitState.HALF_OPEN, "cooldown elapsed, testing recovery"
                )
                # fall through to the HALF_OPEN branch below
            else:
                raise CircuitOpenError(key, entry.state, self._config.cooldown_seconds - elapsed)

        if entry.state == CircuitState.HALF_OPEN:
            if entry.half_open_trials_in_flight >= self._config.half_open_max_trials:
                raise CircuitOpenError(key, entry.state, None)
            entry.half_open_trials_in_flight += 1
            return

        # CLOSED: always allowed, nothing to reserve.
        return

    def record_outcome(self, key: str, success: bool) -> None:
        entry = self._entry(key)

        if entry.state == CircuitState.HALF_OPEN:
            entry.half_open_trials_in_flight = max(0, entry.half_open_trials_in_flight - 1)
            if success:
                entry.consecutive_failures = 0
                self._transition(key, entry, CircuitState.CLOSED, "trial request succeeded")
            else:
                entry.consecutive_failures += 1
                entry.opened_at = time.time()
                self._transition(key, entry, CircuitState.OPEN, "trial request failed, reopening")
            return

        if success:
            entry.consecutive_failures = 0
            return

        entry.consecutive_failures += 1
        if (
            entry.state == CircuitState.CLOSED
            and entry.consecutive_failures >= self._config.failure_threshold
        ):
            entry.opened_at = time.time()
            self._transition(
                key, entry, CircuitState.OPEN,
                f"{entry.consecutive_failures} consecutive failures "
                f">= threshold {self._config.failure_threshold}",
            )

    def snapshot(self, key: str) -> dict:
        entry = self._entry(key)
        return {
            "key": key,
            "state": entry.state.value,
            "consecutive_failures": entry.consecutive_failures,
            "opened_at": entry.opened_at,
            "last_transition_at": entry.last_transition_at,
        }

    def all_snapshots(self) -> list[dict]:
        return [self.snapshot(key) for key in self._entries]


_breaker: CircuitBreaker | None = None


def build_circuit_breaker(config: CircuitBreakerConfig) -> CircuitBreaker:
    global _breaker
    _breaker = CircuitBreaker(config)
    return _breaker


def get_circuit_breaker() -> CircuitBreaker:
    if _breaker is None:
        raise RuntimeError("Circuit breaker not initialized yet")
    return _breaker
