"""
Budget cap tests (Step 17).

team-a: daily_limit_usd=50.0. Rather than firing 50-dollars-worth of mock
requests to organically hit the cap (slow and indirect), most of these
tests pre-load Redis with the exact spend state that matters, the same way
Step 6's manual verification did - the point under test is the gateway's
*reaction* to a given spend state, not the arithmetic that produced it
(that's app/budget.py's own unit-tested cost calculation).
"""
import time

from tests.conftest import TEAM_A_KEY, auth_headers, chat_payload


def _daily_budget_key(team: str) -> str:
    day = time.strftime("%Y-%m-%d", time.gmtime())
    return f"budget:{team}:daily:{day}"


async def test_request_allowed_under_cap(gateway, redis_client):
    await redis_client.set(_daily_budget_key("team-a"), "10.0")  # well under $50 cap
    resp = await gateway.post(
        "/v1/chat/completions", json=chat_payload(), headers=auth_headers(TEAM_A_KEY)
    )
    assert resp.status_code == 200


async def test_request_blocked_at_cap(gateway, redis_client):
    await redis_client.set(_daily_budget_key("team-a"), "50.0")  # exactly at the $50 cap
    resp = await gateway.post(
        "/v1/chat/completions", json=chat_payload(), headers=auth_headers(TEAM_A_KEY)
    )
    assert resp.status_code == 402
    assert "budget" in resp.json()["detail"].lower()


async def test_request_blocked_over_cap(gateway, redis_client):
    await redis_client.set(_daily_budget_key("team-a"), "75.0")  # over the $50 cap
    resp = await gateway.post(
        "/v1/chat/completions", json=chat_payload(), headers=auth_headers(TEAM_A_KEY)
    )
    assert resp.status_code == 402


async def test_successful_requests_accumulate_spend(gateway, redis_client):
    # No pre-loaded spend - start clean and confirm real requests actually
    # increment the budget counter by a real, calculated (non-zero) amount.
    for _ in range(3):
        resp = await gateway.post(
            "/v1/chat/completions", json=chat_payload(), headers=auth_headers(TEAM_A_KEY)
        )
        assert resp.status_code == 200

    spend = await redis_client.get(_daily_budget_key("team-a"))
    assert spend is not None
    assert float(spend) > 0


async def test_unrestricted_team_never_blocked_by_budget(gateway, redis_client):
    from tests.conftest import TEAM_C_KEY

    # team-c has no budget configured at all - no cap should ever apply,
    # regardless of how much "spend" ends up recorded for it.
    await redis_client.set(_daily_budget_key("team-c"), "999999.0")
    resp = await gateway.post(
        "/v1/chat/completions", json=chat_payload(), headers=auth_headers(TEAM_C_KEY)
    )
    assert resp.status_code == 200
