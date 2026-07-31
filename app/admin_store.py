"""
Runtime admin overrides for a team's rate limit, budget, and priority.

Stored in Redis so they're consistent across gateway processes/instances
(same reasoning as Step 5/6's Redis-backed state) and survive a process
restart, unlike an in-memory dict. config.yaml remains the source of truth
for identity/auth/model access (api_key, allowed_models, allowed_providers,
policy) - only rate_limit, budget, and priority can be adjusted live via
the admin API, without redeploying.

Override semantics (see app/routers/admin.py for the endpoints that drive
this):
  - PATCH sets an explicit override for whichever fields are present in the
    request body. A field explicitly set to `null` means "override this
    team to have no limit" (e.g. rate_limit: null -> unlimited) - that is
    a deliberate admin decision, distinct from...
  - DELETE clears all overrides for a team, reverting it to whatever
    config.yaml says - "forget the override," not "set it to unlimited."
"""
from __future__ import annotations

import json
from typing import Any

import redis.asyncio as redis

from app.config import BudgetConfig, RateLimitConfig, TeamConfig

_OVERRIDE_KEY_PREFIX = "admin:overrides:"


class AdminStore:
    def __init__(self, redis_url: str):
        self._redis = redis.from_url(redis_url, decode_responses=True)

    async def close(self) -> None:
        await self._redis.aclose()

    async def get_overrides(self, team_name: str) -> dict[str, Any]:
        raw = await self._redis.get(f"{_OVERRIDE_KEY_PREFIX}{team_name}")
        return json.loads(raw) if raw else {}

    async def set_overrides(self, team_name: str, overrides: dict[str, Any]) -> None:
        await self._redis.set(f"{_OVERRIDE_KEY_PREFIX}{team_name}", json.dumps(overrides))

    async def clear_overrides(self, team_name: str) -> None:
        await self._redis.delete(f"{_OVERRIDE_KEY_PREFIX}{team_name}")

    async def effective_team(self, team: TeamConfig) -> TeamConfig:
        """Returns a copy of `team` with rate_limit/budget/priority replaced
        by any admin overrides. Everything else (api_key, allowed_models,
        allowed_providers, policy) is untouched - those stay config.yaml-only."""
        overrides = await self.get_overrides(team.name)
        if not overrides:
            return team

        update: dict[str, Any] = {}
        if "rate_limit" in overrides:
            value = overrides["rate_limit"]
            update["rate_limit"] = RateLimitConfig(**value) if value else None
        if "budget" in overrides:
            value = overrides["budget"]
            update["budget"] = BudgetConfig(**value) if value else None
        if "priority" in overrides:
            update["priority"] = overrides["priority"]

        return team.model_copy(update=update)


_store: AdminStore | None = None


def build_admin_store(redis_url: str) -> AdminStore:
    global _store
    _store = AdminStore(redis_url)
    return _store


def get_admin_store() -> AdminStore:
    if _store is None:
        raise RuntimeError("Admin store not initialized yet")
    return _store
