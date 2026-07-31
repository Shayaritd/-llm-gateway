"""
Fallback activation tests (Step 17).

config.yaml wires gpt-4o-mini <-> claude-3-5-sonnet as each other's
fallback. These tests replace the mock providers per-test to force a
primary failure and confirm the fallback candidate serves the request -
and, just as importantly, that fallback never grants a team access to a
model it isn't otherwise authorized for.
"""
from tests.conftest import (
    TEAM_A_KEY,
    TEAM_B_KEY,
    TEAM_C_KEY,
    FailsProvider,
    WorksProvider,
    auth_headers,
    chat_payload,
)
from app.providers.registry import get_registry


async def test_fallback_serves_request_when_primary_down(gateway):
    get_registry()._providers["openai"] = FailsProvider("openai", status_code=503)
    get_registry()._providers["anthropic"] = WorksProvider("anthropic")

    resp = await gateway.post(
        "/v1/chat/completions", json=chat_payload(model="gpt-4o-mini"), headers=auth_headers(TEAM_A_KEY)
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] == "claude-3-5-sonnet"
    assert data["provider"] == "anthropic"
    assert data["requested_model"] == "gpt-4o-mini"
    assert data["fallback_attempts"]
    assert data["fallback_attempts"][0]["model"] == "gpt-4o-mini"


async def test_no_fallback_metadata_when_primary_succeeds(gateway):
    resp = await gateway.post(
        "/v1/chat/completions", json=chat_payload(model="gpt-4o-mini"), headers=auth_headers(TEAM_A_KEY)
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] == "gpt-4o-mini"
    assert data.get("requested_model") is None
    assert not data.get("fallback_attempts")


async def test_fallback_blocked_by_allowed_models(gateway):
    # team-b is only allowed gpt-4o-mini - claude-3-5-sonnet (its configured
    # fallback) must never be used for team-b, even if openai is down.
    get_registry()._providers["openai"] = FailsProvider("openai", status_code=503)
    get_registry()._providers["anthropic"] = WorksProvider("anthropic")

    resp = await gateway.post(
        "/v1/chat/completions", json=chat_payload(model="gpt-4o-mini"), headers=auth_headers(TEAM_B_KEY)
    )
    assert resp.status_code == 502  # no authorized candidate left to try


async def test_fallback_blocked_by_allowed_providers(gateway):
    # team-c: allowed_models includes claude-3-5-sonnet, but
    # allowed_providers=[openai] blocks it anyway - a second, independent
    # axis that must also gate fallback candidates.
    get_registry()._providers["openai"] = FailsProvider("openai", status_code=503)
    get_registry()._providers["anthropic"] = WorksProvider("anthropic")

    resp = await gateway.post(
        "/v1/chat/completions", json=chat_payload(model="gpt-4o-mini"), headers=auth_headers(TEAM_C_KEY)
    )
    assert resp.status_code == 502


async def test_non_retryable_failure_skips_straight_to_fallback(gateway):
    # A 400 (invalid request) shouldn't burn retries against a candidate
    # that will just fail the same way again - it should move to the next
    # candidate immediately.
    openai = FailsProvider("openai", status_code=400)
    get_registry()._providers["openai"] = openai
    get_registry()._providers["anthropic"] = WorksProvider("anthropic")

    resp = await gateway.post(
        "/v1/chat/completions", json=chat_payload(model="gpt-4o-mini"), headers=auth_headers(TEAM_A_KEY)
    )
    assert resp.status_code == 200
    assert openai.call_count == 1  # no retries wasted
    assert resp.json()["fallback_attempts"][0]["attempts"] == 1


async def test_all_candidates_failing_returns_502_with_details(gateway):
    get_registry()._providers["openai"] = FailsProvider("openai", status_code=503)
    get_registry()._providers["anthropic"] = FailsProvider("anthropic", status_code=503)

    resp = await gateway.post(
        "/v1/chat/completions", json=chat_payload(model="gpt-4o-mini"), headers=auth_headers(TEAM_A_KEY)
    )
    assert resp.status_code == 502
    assert "gpt-4o-mini" in resp.json()["detail"]
    assert "claude-3-5-sonnet" in resp.json()["detail"]
