"""
Usage accounting and budget enforcement.

Tracks cumulative USD spend per team over rolling calendar day/month
buckets in Redis, using atomic INCRBYFLOAT so concurrent requests across
processes/instances all see a consistent running total - the same
distributed-correctness requirement as Step 5's rate limiter, just with
simple atomic increments instead of a Lua script (there's no
check-then-conditionally-deduct race here: enforcement reads the total
*before* dispatch, and recording only ever adds after dispatch).

Cost is calculated from *actual* provider usage on the non-streaming path
(Usage.prompt_tokens/completion_tokens from the response), and from the
same character-based heuristic used for Step 5's TPM estimate on the
streaming path, since usage isn't reliably available on every provider's
stream. This is a documented approximation, not a limitation unique to
this step.

Enforcement happens in two places:
  - before dispatch: reject the request if the team is already at/over its
    daily or monthly cap.
  - after dispatch: record actual (or estimated, for streaming) cost, and
    log a warning if the running total crosses 80% of either cap.

Explicitly NOT in scope: an admin dashboard for viewing spend, or wiring
the 80% warning into a real alerting/notification system - those are later.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import redis.asyncio as redis

from app.config import BudgetConfig, ModelPricing

logger = logging.getLogger("llm_gateway.budget")

_DAILY_KEY_TTL = 60 * 60 * 24 * 2  # 2 days
_MONTHLY_KEY_TTL = 60 * 60 * 24 * 32  # ~32 days
WARNING_THRESHOLD = 0.8


class BudgetExceeded(Exception):
    def __init__(self, period: str, limit: float, spent: float):
        self.period = period  # "daily" or "monthly"
        self.limit = limit
        self.spent = spent
        super().__init__(f"{period} budget exceeded: {spent:.4f} / {limit:.4f} USD spent")


def calculate_cost(
    pricing: ModelPricing | None, prompt_tokens: int, completion_tokens: int
) -> float:
    """Returns 0.0 for models with no configured pricing, rather than failing
    the request - an unpriced model just isn't tracked against budget."""
    if pricing is None:
        return 0.0
    prompt_cost = (prompt_tokens / 1000) * pricing.input_cost_per_1k
    completion_cost = (completion_tokens / 1000) * pricing.output_cost_per_1k
    return prompt_cost + completion_cost


def _daily_key(team_name: str) -> str:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"budget:{team_name}:daily:{day}"


def _monthly_key(team_name: str) -> str:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    return f"budget:{team_name}:monthly:{month}"


class BudgetTracker:
    def __init__(self, redis_url: str):
        self._redis = redis.from_url(redis_url, decode_responses=True)

    async def close(self) -> None:
        await self._redis.aclose()

    async def get_spend(self, team_name: str) -> tuple[float, float]:
        daily_raw, monthly_raw = await self._redis.mget(
            _daily_key(team_name), _monthly_key(team_name)
        )
        return float(daily_raw or 0.0), float(monthly_raw or 0.0)

    async def check_budget(self, team_name: str, budget: BudgetConfig | None) -> None:
        """Raises BudgetExceeded if the team is already at/over a configured cap."""
        if budget is None:
            return
        daily_spent, monthly_spent = await self.get_spend(team_name)
        if budget.daily_limit_usd is not None and daily_spent >= budget.daily_limit_usd:
            raise BudgetExceeded("daily", budget.daily_limit_usd, daily_spent)
        if budget.monthly_limit_usd is not None and monthly_spent >= budget.monthly_limit_usd:
            raise BudgetExceeded("monthly", budget.monthly_limit_usd, monthly_spent)

    async def record_usage(
        self, team_name: str, budget: BudgetConfig | None, cost: float
    ) -> None:
        if cost <= 0:
            return

        daily_key = _daily_key(team_name)
        monthly_key = _monthly_key(team_name)

        new_daily = await self._redis.incrbyfloat(daily_key, cost)
        await self._redis.expire(daily_key, _DAILY_KEY_TTL)
        new_monthly = await self._redis.incrbyfloat(monthly_key, cost)
        await self._redis.expire(monthly_key, _MONTHLY_KEY_TTL)

        if budget is None:
            return

        if (
            budget.daily_limit_usd is not None
            and new_daily >= WARNING_THRESHOLD * budget.daily_limit_usd
        ):
            logger.warning(
                "budget_warning team=%s period=daily spent=%.4f limit=%.4f pct=%.0f",
                team_name,
                new_daily,
                budget.daily_limit_usd,
                100 * new_daily / budget.daily_limit_usd,
            )
        if (
            budget.monthly_limit_usd is not None
            and new_monthly >= WARNING_THRESHOLD * budget.monthly_limit_usd
        ):
            logger.warning(
                "budget_warning team=%s period=monthly spent=%.4f limit=%.4f pct=%.0f",
                team_name,
                new_monthly,
                budget.monthly_limit_usd,
                100 * new_monthly / budget.monthly_limit_usd,
            )


_tracker: BudgetTracker | None = None


def build_budget_tracker(redis_url: str) -> BudgetTracker:
    global _tracker
    _tracker = BudgetTracker(redis_url)
    return _tracker


def get_budget_tracker() -> BudgetTracker:
    if _tracker is None:
        raise RuntimeError("Budget tracker not initialized yet")
    return _tracker
