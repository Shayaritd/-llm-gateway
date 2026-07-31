"""
Concurrent rate limit tests (Step 17).

team-b's config.yaml limit is rpm=5, tpm=2000 - tight on purpose so this
is fast to exercise. The key thing under test is that the limit holds up
under real concurrency (asyncio.gather), not just sequential calls - the
whole point of Step 5's atomic Lua script was correctness under race
conditions, so these tests fire everything at once.
"""
import asyncio

import pytest

from tests.conftest import TEAM_B_KEY, auth_headers, chat_payload


async def test_concurrent_requests_exactly_rpm_succeed(gateway):
    # team-b: rpm=5. Fire 15 concurrent requests; exactly 5 should reach the
    # provider (200) and the rest should be rejected (429).
    responses = await asyncio.gather(*[
        gateway.post("/v1/chat/completions", json=chat_payload(), headers=auth_headers(TEAM_B_KEY))
        for _ in range(15)
    ])
    status_codes = [r.status_code for r in responses]
    assert status_codes.count(200) == 5, f"expected exactly 5 successes, got {status_codes}"
    assert status_codes.count(429) == 10, f"expected exactly 10 rejections, got {status_codes}"


async def test_rate_limited_response_has_retry_after_header(gateway):
    responses = await asyncio.gather(*[
        gateway.post("/v1/chat/completions", json=chat_payload(), headers=auth_headers(TEAM_B_KEY))
        for _ in range(10)
    ])
    rejected = [r for r in responses if r.status_code == 429]
    assert rejected, "expected at least one 429 among 10 concurrent requests against rpm=5"
    for r in rejected:
        assert "retry-after" in r.headers
        assert int(r.headers["retry-after"]) >= 1


async def test_different_teams_have_independent_limits(gateway):
    from tests.conftest import TEAM_A_KEY

    # Exhaust team-b's limit...
    await asyncio.gather(*[
        gateway.post("/v1/chat/completions", json=chat_payload(), headers=auth_headers(TEAM_B_KEY))
        for _ in range(5)
    ])
    # ...team-a (rpm=60, unaffected) must still succeed.
    resp = await gateway.post(
        "/v1/chat/completions", json=chat_payload(), headers=auth_headers(TEAM_A_KEY)
    )
    assert resp.status_code == 200
