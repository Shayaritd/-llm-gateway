"""
Chat completion endpoint. Supports non-streaming (Step 1/2 behavior) and
streaming passthrough (Step 3), request enrichment / policy injection
(Step 4), Redis token bucket rate limiting (Step 5), budget enforcement
(Step 6), priority-based admission control (Step 7), live admin overrides
for rate limit/budget/priority (Step 8), automatic fallback routing
with per-candidate retry (Steps 10/11), per-model circuit breakers
(Step 12), and OpenTelemetry tracing (Step 13).

Common flow:
  1. Read team attached by TeamAuthMiddleware, then merge in any live admin
     overrides via app.admin_store.effective_team - auth, model access, and
     policy stay config.yaml-only; only rate_limit/budget/priority can be
     adjusted at runtime through the admin API.
  2. Resolve + authorize the ORIGINAL requested model via
     app.routing.resolve_route (this is what produces a clean 403/404 if
     the team can't use what it asked for at all).
  3. Apply team policy via app.policy.apply_request_policy: rejects the
     request outright if it hits the pre-provider content filter, otherwise
     returns a (possibly rewritten) request with the team's system prompt
     merged in.
  4. Enforce the team's RPM/TPM rate limits via app.ratelimit - a single
     atomic Redis operation that checks and consumes both buckets, or
     neither. Rejected requests get 429 + Retry-After.
  5. Enforce the team's daily/monthly budget cap via app.budget - reject if
     the team is already at/over a configured cap (402).
  6. Acquire an admission slot via app.admission, using the team's
     config-driven priority tier. Held for the whole fallback/retry
     sequence below, not re-acquired per candidate.
  7. Build the fallback candidate chain via app.fallback.build_candidates,
     then dispatch via app.fallback.dispatch_with_fallback, which checks
     each candidate's circuit breaker (Step 12) and gives it its own retry
     budget (Step 11) before moving to the next one.
  8a. Non-streaming: whichever candidate wins, record actual cost, tag the
      response with served/requested model metadata, append the
      disclaimer, release the admission slot, and return it.
  8b. Streaming: fallback/retry only applies to ESTABLISHING the stream and
      getting its first chunk - once real content has started flowing,
      headers are committed and a failure can only be surfaced as an SSE
      error event, not silently retried on a different provider.

Tracing (Step 13): a root span "gateway.request_receipt" wraps the whole
request, with child spans for exactly the phases this step asks for -
"gateway.auth", "gateway.routing", "gateway.rate_limit_check",
"gateway.provider_selection", "gateway.provider_call",
"gateway.response_processing", "gateway.response_delivery" - tagged with
team, requested/served model, tokens, cost, and latency attributes.
Phases not explicitly listed for this step (policy application, budget
check, admission control) still run, just without their own named span -
only the root span sees them as part of its own total duration. For
streaming, "response_processing" and "response_delivery" are merged into
one "gateway.response_delivery" span that stays open for the whole SSE
send (there's no distinct processing step before delivery starts when
output is generated incrementally); it's created before the first byte is
sent and ended - together with the root span - in the stream generator's
`finally`, once the connection closes. Any exception exits its span's
`with` block, which the OpenTelemetry SDK automatically records as an
error status - no manual recording is needed - except for the root span,
which is managed manually (via record_exception/set_status/end) since it
must survive past the point where a streaming response is returned.

Explicitly NOT in scope here: Grafana dashboard JSON, alerting on trace
data, metrics (this step is tracing only), real moderation provider
integration, admin dashboards.
"""
from __future__ import annotations

import json
import math
import time
from typing import AsyncIterator

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode, Tracer

from app.admin_store import get_admin_store
from app.admission import AdmissionController, AdmissionTimeout, get_admission_controller
from app.budget import BudgetExceeded, BudgetTracker, calculate_cost, get_budget_tracker
from app.circuit_breaker import get_circuit_breaker
from app.config import GatewayConfig, TeamConfig, get_config
from app.fallback import (
    AllAttemptsFailed,
    FallbackCandidate,
    build_candidates,
    dispatch_with_fallback,
)
from app.hooks import on_stream_complete
from app.metrics import (
    COST_USD_TOTAL,
    ERRORS_TOTAL,
    FALLBACK_TRIGGERED_TOTAL,
    REQUEST_DURATION_SECONDS,
    REQUESTS_TOTAL,
    TOKENS_TOTAL,
    error_type_for_status,
    set_team_budget_gauges,
)
from app.policy import PolicyError, append_disclaimer, apply_request_policy
from app.providers.base import ProviderError
from app.providers.registry import get_registry
from app.ratelimit import RateLimitExceeded, enforce_rate_limit, get_rate_limiter
from app.routing import RoutingError, resolve_route
from app.schemas import (
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
    ChatCompletionRequest,
)
from app.tokens import estimate_completion_tokens, estimate_prompt_tokens, estimate_text_tokens
from app.tracing import get_tracer

router = APIRouter()


def _child_span(tracer: Tracer, name: str, parent: Span):
    """Starts `name` as a child of `parent` explicitly, rather than relying
    on ambient context-manager nesting - needed because the root span has
    to survive past this function's return for streaming responses (see
    module docstring), so it can't itself be a `with`-managed context for
    the whole request."""
    return tracer.start_as_current_span(name, context=trace.set_span_in_context(parent))


async def _call_non_streaming(candidate: FallbackCandidate, body: ChatCompletionRequest):
    return await candidate.provider.chat_completion(body, candidate.provider_model)


async def _get_first_chunk(candidate: FallbackCandidate, body: ChatCompletionRequest):
    """Establishes the stream for this candidate and pulls its first chunk.
    A failure here (connection error, auth error, etc.) is exactly what
    app.retry/app.fallback need to see in order to retry or move to the
    next candidate - it's indistinguishable from a non-streaming failure
    at this point, since no content has reached the client yet."""
    agen = candidate.provider.chat_completion_stream(body, candidate.provider_model)
    try:
        first_chunk = await agen.__anext__()
    except StopAsyncIteration:
        first_chunk = None
    return agen, first_chunk


def _tag_chunk(chunk: ChatCompletionChunk, served_model: str, requested_model: str) -> ChatCompletionChunk:
    return chunk.model_copy(
        update={
            "model": served_model,
            "requested_model": requested_model if served_model != requested_model else None,
        }
    )


async def _stream_response(
    agen: AsyncIterator[ChatCompletionChunk],
    first_chunk: ChatCompletionChunk | None,
    winning_candidate: FallbackCandidate,
    body: ChatCompletionRequest,
    team: TeamConfig,
    budget_tracker: BudgetTracker,
    admission: AdmissionController,
    config: GatewayConfig,
    tracer: Tracer,
    root_span: Span,
    request_start: float,
) -> AsyncIterator[str]:
    accumulated = ""
    finish_reason: str | None = None
    response_id = f"{winning_candidate.provider_name}-stream-{int(time.time())}"

    # response_processing + response_delivery merged for streaming - see
    # module docstring. Stays open for the whole SSE send.
    delivery_span = tracer.start_span(
        "gateway.response_delivery", context=trace.set_span_in_context(root_span)
    )

    def tag(c: ChatCompletionChunk) -> ChatCompletionChunk:
        return _tag_chunk(c, winning_candidate.model, body.model)

    try:
        if first_chunk is not None:
            first_chunk = tag(first_chunk)
            response_id = first_chunk.id
            for choice in first_chunk.choices:
                if choice.delta.content:
                    accumulated += choice.delta.content
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
            yield f"data: {first_chunk.model_dump_json()}\n\n"

        # From here on, a failure means fallback is no longer possible -
        # content may already be on the wire. See module docstring.
        async for chunk in agen:
            chunk = tag(chunk)
            response_id = chunk.id
            for choice in chunk.choices:
                if choice.delta.content:
                    accumulated += choice.delta.content
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
            yield f"data: {chunk.model_dump_json()}\n\n"

        disclaimer_text = append_disclaimer(team, "")
        if disclaimer_text:
            accumulated += disclaimer_text
            disclaimer_chunk = ChatCompletionChunk(
                id=response_id,
                model=winning_candidate.model,
                provider=winning_candidate.provider_name,
                created=int(time.time()),
                choices=[
                    ChatCompletionChunkChoice(
                        index=0,
                        delta=ChatCompletionChunkDelta(content=disclaimer_text),
                        finish_reason=finish_reason,
                    )
                ],
                requested_model=body.model if winning_candidate.model != body.model else None,
            )
            yield f"data: {disclaimer_chunk.model_dump_json()}\n\n"

        yield "data: [DONE]\n\n"
    except (ProviderError, httpx.HTTPError) as e:
        delivery_span.record_exception(e)
        delivery_span.set_status(Status(StatusCode.ERROR, str(e)))
        ERRORS_TOTAL.labels(
            team=team.name, model=winning_candidate.model,
            provider=winning_candidate.provider_name, error_type="stream_interrupted",
        ).inc()
        yield f"data: {json.dumps({'error': str(e)})}\n\n"
    finally:
        await admission.release()

        on_stream_complete(
            team=team.name,
            model=winning_candidate.model,
            provider=winning_candidate.provider_name,
            content=accumulated,
            finish_reason=finish_reason,
        )

        duration_seconds = time.monotonic() - request_start
        REQUESTS_TOTAL.labels(
            team=team.name, model=winning_candidate.model, provider=winning_candidate.provider_name
        ).inc()
        REQUEST_DURATION_SECONDS.labels(
            team=team.name, model=winning_candidate.model, provider=winning_candidate.provider_name
        ).observe(duration_seconds)
        if winning_candidate.model != body.model:
            FALLBACK_TRIGGERED_TOTAL.labels(
                team=team.name, requested_model=body.model, served_model=winning_candidate.model
            ).inc()

        prompt_tokens = estimate_prompt_tokens(body)
        delivery_span.set_attribute("llm.tokens.prompt", prompt_tokens)
        if accumulated or finish_reason:
            pricing = config.pricing.get(winning_candidate.model)
            completion_tokens = estimate_text_tokens(accumulated)
            cost = calculate_cost(pricing, prompt_tokens, completion_tokens)
            await budget_tracker.record_usage(team.name, team.budget, cost)
            delivery_span.set_attribute("llm.tokens.completion", completion_tokens)
            delivery_span.set_attribute("llm.cost_usd", cost)
            root_span.set_attribute("llm.tokens.prompt", prompt_tokens)
            root_span.set_attribute("llm.tokens.completion", completion_tokens)
            root_span.set_attribute("llm.cost_usd", cost)

            labels = dict(
                team=team.name, model=winning_candidate.model,
                provider=winning_candidate.provider_name,
            )
            TOKENS_TOTAL.labels(**labels, token_type="prompt").inc(prompt_tokens)
            TOKENS_TOTAL.labels(**labels, token_type="completion").inc(completion_tokens)
            COST_USD_TOTAL.labels(team=team.name, model=winning_candidate.model).inc(cost)

        delivery_span.end()
        root_span.set_attribute("llm.total_latency_ms", duration_seconds * 1000)
        root_span.end()


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, body: ChatCompletionRequest):
    request_start = time.monotonic()
    tracer = get_tracer()
    root_span = tracer.start_span(
        "gateway.request_receipt", attributes={"llm.requested_model": body.model}
    )
    # Tracked across the whole function so the error handler at the bottom
    # can label metrics with whatever was actually known at failure time -
    # "unknown"/"n/a" for anything not yet resolved (e.g. team is unknown
    # if auth itself failed; provider is "n/a" until a candidate wins).
    metrics_team = "unknown"
    metrics_model = body.model
    metrics_provider = "n/a"

    try:
        with _child_span(tracer, "gateway.auth", root_span) as auth_span:
            team = getattr(request.state, "team", None)
            if team is None:
                # Should not happen if TeamAuthMiddleware is installed, but guard anyway.
                raise HTTPException(status_code=401, detail="Missing authenticated team")
            # Merge in any live admin overrides (Step 8) for rate_limit/
            # budget/priority - auth, model access, and policy stay
            # config.yaml-only. See app.admin_store.AdminStore.effective_team.
            team = await get_admin_store().effective_team(team)
            auth_span.set_attribute("team.name", team.name)
        root_span.set_attribute("team.name", team.name)
        metrics_team = team.name

        config = get_config()
        with _child_span(tracer, "gateway.routing", root_span) as routing_span:
            try:
                resolve_route(team, body.model, config)  # authorize the ORIGINAL request
            except RoutingError as e:
                raise HTTPException(status_code=e.status_code, detail=str(e))
            model_route = config.models.get(body.model)
            if model_route is not None:
                routing_span.set_attribute("llm.primary_provider", model_route.provider)

        try:
            body = apply_request_policy(team, body)
        except PolicyError as e:
            raise HTTPException(status_code=e.status_code, detail=str(e))

        tpm_cost = estimate_prompt_tokens(body) + estimate_completion_tokens(body)
        with _child_span(tracer, "gateway.rate_limit_check", root_span) as rl_span:
            rl_span.set_attribute("ratelimit.tpm_cost_estimate", tpm_cost)
            try:
                await enforce_rate_limit(get_rate_limiter(), team.name, team.rate_limit, tpm_cost)
            except RateLimitExceeded as e:
                raise HTTPException(
                    status_code=429,
                    detail=f"{e.limit_type.upper()} rate limit exceeded for team '{team.name}'",
                    headers={"Retry-After": str(max(1, math.ceil(e.retry_after)))},
                )

        budget_tracker = get_budget_tracker()
        set_team_budget_gauges(team.name, team.budget)
        try:
            await budget_tracker.check_budget(team.name, team.budget)
        except BudgetExceeded as e:
            raise HTTPException(
                status_code=402,
                detail=(
                    f"{e.period.capitalize()} budget exceeded for team '{team.name}': "
                    f"${e.spent:.4f} of ${e.limit:.4f} USD spent"
                ),
            )

        admission = get_admission_controller()
        try:
            await admission.acquire(team.priority)
        except AdmissionTimeout as e:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Server busy: {e.priority}-priority request for team '{team.name}' "
                    f"timed out waiting {e.waited_seconds:.1f}s for available capacity"
                ),
                headers={"Retry-After": "1"},
            )

        with _child_span(tracer, "gateway.provider_selection", root_span) as sel_span:
            candidates = build_candidates(body.model, team, config, get_registry())
            sel_span.set_attribute("llm.candidate_count", len(candidates))
        if not candidates:
            await admission.release()
            raise HTTPException(
                status_code=500, detail=f"No available provider candidates for '{body.model}'"
            )

        circuit_breaker = get_circuit_breaker()

        if body.stream:
            with _child_span(tracer, "gateway.provider_call", root_span) as call_span:
                call_start = time.monotonic()
                try:
                    (agen, first_chunk), winning_candidate, prior_failures = await dispatch_with_fallback(
                        candidates, lambda c: _get_first_chunk(c, body), config.retry_policy, circuit_breaker
                    )
                except AllAttemptsFailed as e:
                    await admission.release()
                    raise HTTPException(status_code=502, detail=str(e))
                call_span.set_attribute("llm.served_model", winning_candidate.model)
                call_span.set_attribute("llm.served_provider", winning_candidate.provider_name)
                call_span.set_attribute("llm.fallback_occurred", winning_candidate.model != body.model)
                call_span.set_attribute("llm.attempts_before_success", len(prior_failures))
                call_span.set_attribute("llm.latency_ms", (time.monotonic() - call_start) * 1000)
            root_span.set_attribute("llm.served_model", winning_candidate.model)
            metrics_model = winning_candidate.model
            metrics_provider = winning_candidate.provider_name

            # root_span and the (merged) response_processing/delivery span are
            # ended inside _stream_response's `finally`, once the SSE
            # connection actually closes - not here, since the response
            # object is handed back to the ASGI layer before that happens.
            return StreamingResponse(
                _stream_response(
                    agen, first_chunk, winning_candidate, body, team, budget_tracker,
                    admission, config, tracer, root_span, request_start,
                ),
                media_type="text/event-stream",
            )

        with _child_span(tracer, "gateway.provider_call", root_span) as call_span:
            call_start = time.monotonic()
            try:
                response, winning_candidate, prior_failures = await dispatch_with_fallback(
                    candidates, lambda c: _call_non_streaming(c, body), config.retry_policy, circuit_breaker
                )
            except AllAttemptsFailed as e:
                await admission.release()
                raise HTTPException(status_code=502, detail=str(e))
            call_span.set_attribute("llm.served_model", winning_candidate.model)
            call_span.set_attribute("llm.served_provider", winning_candidate.provider_name)
            call_span.set_attribute("llm.fallback_occurred", winning_candidate.model != body.model)
            call_span.set_attribute("llm.attempts_before_success", len(prior_failures))
            call_span.set_attribute("llm.latency_ms", (time.monotonic() - call_start) * 1000)

        metrics_model = winning_candidate.model
        metrics_provider = winning_candidate.provider_name
        await admission.release()

        with _child_span(tracer, "gateway.response_processing", root_span) as proc_span:
            response.model = winning_candidate.model
            response.provider = winning_candidate.provider_name
            if winning_candidate.model != body.model:
                response.requested_model = body.model
            if prior_failures:
                response.fallback_attempts = [f.__dict__ for f in prior_failures]

            pricing = config.pricing.get(response.model)
            cost = calculate_cost(
                pricing, response.usage.prompt_tokens or 0, response.usage.completion_tokens or 0
            )
            await budget_tracker.record_usage(team.name, team.budget, cost)

            for choice in response.choices:
                choice.message.content = append_disclaimer(team, choice.message.content)

            proc_span.set_attribute("llm.tokens.prompt", response.usage.prompt_tokens or 0)
            proc_span.set_attribute("llm.tokens.completion", response.usage.completion_tokens or 0)
            proc_span.set_attribute("llm.cost_usd", cost)

        root_span.set_attribute("llm.tokens.prompt", response.usage.prompt_tokens or 0)
        root_span.set_attribute("llm.tokens.completion", response.usage.completion_tokens or 0)
        root_span.set_attribute("llm.cost_usd", cost)

        with _child_span(tracer, "gateway.response_delivery", root_span) as delivery_span:
            delivery_span.set_attribute("http.status_code", 200)

        duration_seconds = time.monotonic() - request_start
        root_span.set_attribute("llm.total_latency_ms", duration_seconds * 1000)
        root_span.end()

        REQUESTS_TOTAL.labels(team=team.name, model=metrics_model, provider=metrics_provider).inc()
        REQUEST_DURATION_SECONDS.labels(
            team=team.name, model=metrics_model, provider=metrics_provider
        ).observe(duration_seconds)
        labels = dict(team=team.name, model=metrics_model, provider=metrics_provider)
        TOKENS_TOTAL.labels(**labels, token_type="prompt").inc(response.usage.prompt_tokens or 0)
        TOKENS_TOTAL.labels(**labels, token_type="completion").inc(response.usage.completion_tokens or 0)
        COST_USD_TOTAL.labels(team=team.name, model=metrics_model).inc(cost)
        if winning_candidate.model != body.model:
            FALLBACK_TRIGGERED_TOTAL.labels(
                team=team.name, requested_model=body.model, served_model=winning_candidate.model
            ).inc()

        return response

    except Exception as e:
        root_span.record_exception(e)
        root_span.set_status(Status(StatusCode.ERROR, str(e)))
        duration_seconds = time.monotonic() - request_start
        root_span.set_attribute("llm.total_latency_ms", duration_seconds * 1000)
        root_span.end()

        status_code = e.status_code if isinstance(e, HTTPException) else 500
        error_type = error_type_for_status(status_code)
        REQUESTS_TOTAL.labels(team=metrics_team, model=metrics_model, provider=metrics_provider).inc()
        ERRORS_TOTAL.labels(
            team=metrics_team, model=metrics_model, provider=metrics_provider, error_type=error_type
        ).inc()
        REQUEST_DURATION_SECONDS.labels(
            team=metrics_team, model=metrics_model, provider=metrics_provider
        ).observe(duration_seconds)
        raise
