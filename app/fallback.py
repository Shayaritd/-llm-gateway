"""
Automatic fallback routing (Step 10).

Builds the ordered list of candidates to attempt for a request - the
originally requested model first, then its configured fallback_chain
(app.config.ModelRoute.fallback_chain) - filtered down to whatever the
team is actually authorized to use. Fallback NEVER grants a team access to
a model it couldn't already call directly: each candidate is passed
through the exact same app.routing.resolve_route checks (allowed_models,
allowed_providers) as the original request. A team with a narrow allowlist
may end up with a fallback chain of length 1 (no usable fallback at all),
which is the correct, safe behavior, not a bug.

Each candidate gets its own retry budget via app.retry.call_with_retry
(Step 11) - only once that budget is exhausted (or the failure is
non-retryable) does dispatch_with_fallback move to the next candidate.
This module has no retry logic of its own; it only sequences candidates
and delegates the "try this one, retry per policy" mechanics to app.retry.

Before attempting each candidate, its circuit breaker (app.circuit_breaker,
Step 12) is checked: an OPEN circuit skips the candidate entirely - no
provider call, no retry budget spent - and it's recorded as a failure with
zero attempts, distinct from a real provider failure. After a candidate is
actually attempted, its outcome (success, or failure once retries are
exhausted) is fed back into the breaker exactly once per candidate-attempt,
not once per raw HTTP call - so a single client request's own retries
don't alone trip the breaker, but repeated failures across many separate
requests do.

Explicitly NOT in scope: Grafana/metrics export.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

import httpx

from app.circuit_breaker import CircuitBreaker, CircuitOpenError
from app.config import GatewayConfig, RetryPolicyConfig, TeamConfig
from app.providers.base import Provider, ProviderError
from app.providers.registry import ProviderRegistry
from app.retry import RetryExhausted, call_with_retry
from app.routing import RoutingError, resolve_route

logger = logging.getLogger("llm_gateway.fallback")

T = TypeVar("T")


@dataclass
class FallbackCandidate:
    model: str  # logical model name
    provider_name: str
    provider: Provider
    provider_model: str


@dataclass
class AttemptFailure:
    model: str
    provider: str
    attempts: int
    error: str


class AllAttemptsFailed(Exception):
    def __init__(self, failures: list[AttemptFailure]):
        self.failures = failures
        detail = "; ".join(f"{f.model}({f.provider}): {f.error}" for f in failures)
        super().__init__(f"all {len(failures)} candidate(s) failed: {detail}")


def build_candidates(
    requested_model: str,
    team: TeamConfig,
    config: GatewayConfig,
    registry: ProviderRegistry,
) -> list[FallbackCandidate]:
    """Requested model first, then its fallback_chain, filtered to models
    the team is authorized for. Duplicates (e.g. a chain that loops back)
    are dropped after their first occurrence."""
    route_config = config.models.get(requested_model)
    if route_config is None:
        return []

    chain = [requested_model, *route_config.fallback_chain]
    candidates: list[FallbackCandidate] = []
    seen: set[str] = set()

    for model_name in chain:
        if model_name in seen:
            continue
        seen.add(model_name)

        try:
            route = resolve_route(team, model_name, config)
        except RoutingError as e:
            logger.info(
                "skipping fallback candidate model=%s for team=%s (not authorized): %s",
                model_name, team.name, e,
            )
            continue

        provider = registry.get(route.provider)
        if provider is None:
            continue

        candidates.append(
            FallbackCandidate(
                model=model_name,
                provider_name=route.provider,
                provider=provider,
                provider_model=route.provider_model,
            )
        )

    return candidates


async def dispatch_with_fallback(
    candidates: list[FallbackCandidate],
    call: Callable[[FallbackCandidate], Awaitable[T]],
    retry_policy: RetryPolicyConfig,
    circuit_breaker: CircuitBreaker,
) -> tuple[T, FallbackCandidate, list[AttemptFailure]]:
    """Tries each candidate in order, giving each its own retry budget via
    call_with_retry, gated by its circuit breaker. Returns (result,
    winning_candidate, prior_failures) on success. Raises AllAttemptsFailed
    if every candidate is exhausted or circuit-open."""
    failures: list[AttemptFailure] = []

    for candidate in candidates:
        description = f"model={candidate.model} provider={candidate.provider_name}"

        try:
            circuit_breaker.check_and_reserve(candidate.model)
        except CircuitOpenError as e:
            logger.warning("skipping candidate, %s", e)
            failures.append(
                AttemptFailure(
                    model=candidate.model, provider=candidate.provider_name, attempts=0,
                    error=f"circuit breaker {e.state.value}, no provider call attempted",
                )
            )
            continue

        try:
            result = await call_with_retry(
                lambda c=candidate: call(c), retry_policy, description=description
            )
            circuit_breaker.record_outcome(candidate.model, success=True)
            return result, candidate, failures
        except RetryExhausted as e:
            circuit_breaker.record_outcome(candidate.model, success=False)
            failures.append(
                AttemptFailure(
                    model=candidate.model,
                    provider=candidate.provider_name,
                    attempts=e.attempts,
                    error=str(e.last_error),
                )
            )
        except (ProviderError, httpx.HTTPError) as e:
            # Non-retryable failure on the first attempt - no retry budget
            # was consumed, but this candidate is still done.
            circuit_breaker.record_outcome(candidate.model, success=False)
            failures.append(
                AttemptFailure(
                    model=candidate.model, provider=candidate.provider_name,
                    attempts=1, error=str(e),
                )
            )

        logger.warning("candidate exhausted, moving to next fallback: %s", description)

    raise AllAttemptsFailed(failures)
