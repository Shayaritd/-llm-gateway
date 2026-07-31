"""
Circuit breaker lifecycle tests (Step 17): closed -> open -> half-open ->
closed/open again, driven through real HTTP requests rather than calling
app.circuit_breaker directly, so this also proves the wiring into
app/fallback.py and app/routers/chat.py is correct end-to-end.

config.yaml's circuit_breaker.failure_threshold=3, cooldown_seconds=10.0
is too slow for a test to sleep through comfortably, so these tests build
their own CircuitBreaker with a short cooldown and swap it into the
gateway's singleton - same pattern as swapping mock providers into the
registry.
"""
import asyncio
import time

import pytest

from tests.conftest import TEAM_A_KEY, FailsProvider, WorksProvider, auth_headers, chat_payload
from app.providers.registry import get_registry
from app.config import CircuitBreakerConfig, get_config
from app.circuit_breaker import CircuitBreaker
import app.circuit_breaker as circuit_breaker_module


@pytest.fixture(autouse=True)
def fast_retries():
    """These tests use a very short circuit-breaker cooldown to stay fast,
    which only makes sense if failing requests themselves resolve quickly
    too - the real config.yaml retry policy (3 retries, real backoff
    sleeps) would take ~3.5s per failing request (retrying both the
    primary and its fallback candidate), which would blow past a
    sub-second cooldown before the "still open" assertion even runs. This
    doesn't change what's under test (the circuit breaker's own state
    machine) - app.retry's backoff behavior already has its own dedicated
    tests."""
    config = get_config()
    original = (
        config.retry_policy.max_retries,
        config.retry_policy.backoff_base_seconds,
        config.retry_policy.backoff_max_seconds,
    )
    config.retry_policy.max_retries = 0
    config.retry_policy.backoff_base_seconds = 0.01
    config.retry_policy.backoff_max_seconds = 0.01
    yield
    (
        config.retry_policy.max_retries,
        config.retry_policy.backoff_base_seconds,
        config.retry_policy.backoff_max_seconds,
    ) = original


async def test_full_lifecycle_closed_open_half_open_closed(gateway):
    # Fast breaker just for this test: trips after 2 failures, 0.3s cooldown.
    fast_cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=2, cooldown_seconds=0.3, half_open_max_trials=1))
    circuit_breaker_module._breaker = fast_cb

    failing = FailsProvider("openai", status_code=503)
    get_registry()._providers["openai"] = failing
    get_registry()._providers["anthropic"] = FailsProvider("anthropic", status_code=503)  # no usable fallback

    # 1. CLOSED: two separate failing requests trip the breaker.
    for _ in range(2):
        resp = await gateway.post(
            "/v1/chat/completions", json=chat_payload(model="gpt-4o-mini"), headers=auth_headers(TEAM_A_KEY)
        )
        assert resp.status_code == 502

    breaker_status = fast_cb.snapshot("gpt-4o-mini")
    assert breaker_status["state"] == "open"

    # 2. OPEN: the provider must NOT be called again while open.
    calls_before = failing.call_count
    resp = await gateway.post(
        "/v1/chat/completions", json=chat_payload(model="gpt-4o-mini"), headers=auth_headers(TEAM_A_KEY)
    )
    assert resp.status_code == 502
    assert failing.call_count == calls_before  # circuit skipped it - zero additional calls
    # A 502 body is a plain HTTPException ({"detail": ...}) - it never has a
    # fallback_attempts field (that only exists on a successful response) -
    # so we assert on the detail message the circuit-skip path produces.
    assert "circuit breaker open, no provider call attempted" in resp.json()["detail"]

    # 3. Wait for cooldown, then swap in a working provider - the HALF_OPEN
    #    trial should succeed and close the circuit.
    await asyncio.sleep(0.35)
    get_registry()._providers["openai"] = WorksProvider("openai")

    resp = await gateway.post(
        "/v1/chat/completions", json=chat_payload(model="gpt-4o-mini"), headers=auth_headers(TEAM_A_KEY)
    )
    assert resp.status_code == 200
    assert fast_cb.snapshot("gpt-4o-mini")["state"] == "closed"


async def test_circuit_breaker_status_endpoint_reflects_state(gateway):
    fast_cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, cooldown_seconds=30, half_open_max_trials=1))
    circuit_breaker_module._breaker = fast_cb

    get_registry()._providers["openai"] = FailsProvider("openai", status_code=503)
    get_registry()._providers["anthropic"] = FailsProvider("anthropic", status_code=503)

    await gateway.post(
        "/v1/chat/completions", json=chat_payload(model="gpt-4o-mini"), headers=auth_headers(TEAM_A_KEY)
    )

    resp = await gateway.get("/health/circuit-breakers")
    assert resp.status_code == 200
    snapshots = resp.json()
    gpt_entry = next(s for s in snapshots if s["key"] == "gpt-4o-mini")
    assert gpt_entry["state"] == "open"


async def test_half_open_trial_failure_reopens_circuit(gateway):
    fast_cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, cooldown_seconds=0.2, half_open_max_trials=1))
    circuit_breaker_module._breaker = fast_cb

    get_registry()._providers["openai"] = FailsProvider("openai", status_code=503)
    get_registry()._providers["anthropic"] = FailsProvider("anthropic", status_code=503)

    await gateway.post(  # trips it
        "/v1/chat/completions", json=chat_payload(model="gpt-4o-mini"), headers=auth_headers(TEAM_A_KEY)
    )
    assert fast_cb.snapshot("gpt-4o-mini")["state"] == "open"

    await asyncio.sleep(0.25)  # cooldown elapses - next request is the half-open trial, and it also fails
    await gateway.post(
        "/v1/chat/completions", json=chat_payload(model="gpt-4o-mini"), headers=auth_headers(TEAM_A_KEY)
    )
    assert fast_cb.snapshot("gpt-4o-mini")["state"] == "open"  # reopened, not closed
