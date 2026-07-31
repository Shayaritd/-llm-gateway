"""
Retry policy abstraction (Step 11).

A small, reusable "retry this async call, with exponential backoff capped
at a max, up to N times, but only for retryable failures" wrapper. It
knows nothing about fallback chains or providers specifically - just how
to retry a single async callable per app.config.RetryPolicyConfig.

app.fallback (Step 10) uses this to retry a single candidate before moving
on to the next one in its fallback chain - that's what "only fallback
after retry policy is exhausted" means concretely: call_with_retry() must
give up on a candidate (raise) before app.fallback tries the next one.

Classification (is_retryable_error): timeouts, rate limiting (429), and
transient upstream failures (5xx, connection errors) are retryable. Auth
failures (401/403), invalid-request errors (400/404/422), and anything
raised for a reason retrying can't fix (e.g. a missing API key, reported
via ProviderError(retryable=False) - see app.providers.base) are not.
Policy rejections (app.policy.PolicyError) never reach this layer at all -
they're rejected before any provider dispatch is attempted, so there's
nothing here to retry.

Explicitly NOT in scope: circuit breakers - this has no memory of past
calls; every call gets its own fresh retry budget, and a provider that
just failed 4 times in a row for the last request gets tried again with a
full retry budget on the next one.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

import httpx

from app.config import RetryPolicyConfig
from app.providers.base import ProviderError

logger = logging.getLogger("llm_gateway.retry")

T = TypeVar("T")


def is_retryable_error(exc: BaseException) -> bool:
    if isinstance(exc, ProviderError):
        return exc.retryable
    if isinstance(exc, httpx.HTTPError):
        # Timeouts, connection failures, transport-level errors - transient
        # by nature; none of these tell us the request itself was invalid.
        return True
    return False


class RetryExhausted(Exception):
    """Raised when every attempt for a single call - the first plus all
    retries - failed with a retryable error. Wraps the last underlying
    error, which is what actually gets shown/logged."""

    def __init__(self, last_error: BaseException, attempts: int):
        self.last_error = last_error
        self.attempts = attempts
        super().__init__(f"gave up after {attempts} attempt(s): {last_error}")


async def call_with_retry(
    fn: Callable[[], Awaitable[T]],
    policy: RetryPolicyConfig,
    *,
    description: str = "call",
) -> T:
    """
    Calls fn(), retrying on retryable failures with exponential backoff
    (base * 2^attempt, capped at policy.backoff_max_seconds), up to
    policy.max_retries additional attempts beyond the first.

    - A non-retryable failure propagates immediately, unwrapped, without
      consuming any retry budget - retrying an invalid request or an auth
      failure would just fail the same way again.
    - If every attempt is retryable but still fails, raises RetryExhausted
      wrapping the last error, once the budget is used up.
    """
    attempt = 0
    while True:
        try:
            return await fn()
        except (ProviderError, httpx.HTTPError) as e:
            if not is_retryable_error(e):
                raise

            if attempt >= policy.max_retries:
                raise RetryExhausted(e, attempts=attempt + 1) from e

            backoff = min(
                policy.backoff_max_seconds, policy.backoff_base_seconds * (2**attempt)
            )
            logger.warning(
                "retrying %s: attempt %d/%d failed with %s, backing off %.2fs",
                description, attempt + 1, policy.max_retries + 1, e, backoff,
            )
            await asyncio.sleep(backoff)
            attempt += 1
