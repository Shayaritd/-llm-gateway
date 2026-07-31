"""
Prometheus metrics (Step 14).

Defines and registers all gateway metrics, rendered by app.routers.metrics
for Prometheus to scrape at /metrics.

Design notes:
  - "RPS" and "token throughput" are Counters, not literal per-second
    values - Prometheus/Grafana compute rate() over these at query time
    (e.g. rate(gateway_requests_total[1m])), the standard way to derive a
    rate from Prometheus. A live "current RPS" gauge we maintained
    ourselves would just be a worse, laggier version of the same thing.
  - "Cost per team/day" is a Counter (gateway_cost_usd_total) accumulating
    total spend; "per day" is a query-time window
    (increase(gateway_cost_usd_total[1d])), same reasoning as above.
  - Circuit breaker state is exposed two ways: a transition Counter (state
    changes, as asked) and a companion state Gauge (current state per
    model) - the natural pairing needed to show "is this model's circuit
    open right now" on a dashboard, not just how often it flips.
  - gateway_team_budget_usd is a small Gauge added specifically to make a
    Prometheus *alert* on "over budget" possible at all (Step 16): a
    Counter alone can't be compared against a cap unless the cap is also
    a metric. Kept updated from whichever code path already has the
    team's effective (possibly admin-overridden) budget config in hand -
    see app/routers/chat.py and app/routers/admin.py.

Explicitly NOT in scope: Grafana dashboards, Prometheus scrape config,
alerting rules - those are Step 15/16.
"""
from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

REGISTRY = CollectorRegistry()

REQUESTS_TOTAL = Counter(
    "gateway_requests_total",
    "Total chat completion requests, labeled by team/model/provider.",
    ["team", "model", "provider"],
    registry=REGISTRY,
)

ERRORS_TOTAL = Counter(
    "gateway_errors_total",
    "Total failed requests, labeled by team/model/provider/error_type.",
    ["team", "model", "provider", "error_type"],
    registry=REGISTRY,
)

REQUEST_DURATION_SECONDS = Histogram(
    "gateway_request_duration_seconds",
    "End-to-end request latency in seconds, labeled by team/model/provider.",
    ["team", "model", "provider"],
    registry=REGISTRY,
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)

TOKENS_TOTAL = Counter(
    "gateway_tokens_total",
    "Total tokens processed, labeled by team/model/provider/token_type (prompt|completion).",
    ["team", "model", "provider", "token_type"],
    registry=REGISTRY,
)

COST_USD_TOTAL = Counter(
    "gateway_cost_usd_total",
    "Total USD cost incurred, labeled by team/model. "
    "Use increase() over a time window (e.g. 1d) for cost-per-period.",
    ["team", "model"],
    registry=REGISTRY,
)

FALLBACK_TRIGGERED_TOTAL = Counter(
    "gateway_fallback_triggered_total",
    "Total requests served by a fallback model instead of the one requested.",
    ["team", "requested_model", "served_model"],
    registry=REGISTRY,
)

CIRCUIT_BREAKER_TRANSITIONS_TOTAL = Counter(
    "gateway_circuit_breaker_transitions_total",
    "Total circuit breaker state transitions, labeled by model/from_state/to_state.",
    ["model", "from_state", "to_state"],
    registry=REGISTRY,
)

CIRCUIT_BREAKER_STATE = Gauge(
    "gateway_circuit_breaker_state",
    "Current circuit breaker state per model: 0=closed, 1=half_open, 2=open.",
    ["model"],
    registry=REGISTRY,
)

TEAM_BUDGET_USD = Gauge(
    "gateway_team_budget_usd",
    "Configured (effective) budget cap per team/period, for alerting on spend vs. cap.",
    ["team", "period"],  # period: "daily" | "monthly"
    registry=REGISTRY,
)

_CIRCUIT_STATE_VALUE = {"closed": 0, "half_open": 1, "open": 2}

_ERROR_TYPE_BY_STATUS = {
    400: "policy_rejected",
    401: "unauthorized",
    402: "budget_exceeded",
    403: "forbidden",
    404: "not_found",
    429: "rate_limited",
    500: "internal_error",
    502: "all_providers_failed",
    503: "admission_timeout",
}


def error_type_for_status(status_code: int) -> str:
    return _ERROR_TYPE_BY_STATUS.get(status_code, "unknown_error")


def record_circuit_breaker_transition(model: str, from_state: str, to_state: str) -> None:
    CIRCUIT_BREAKER_TRANSITIONS_TOTAL.labels(
        model=model, from_state=from_state, to_state=to_state
    ).inc()
    CIRCUIT_BREAKER_STATE.labels(model=model).set(_CIRCUIT_STATE_VALUE.get(to_state, -1))


def set_team_budget_gauges(team_name: str, budget) -> None:
    """budget: app.config.BudgetConfig | None (the team's *effective* budget,
    including any admin override)."""
    daily = budget.daily_limit_usd if budget else None
    monthly = budget.monthly_limit_usd if budget else None
    TEAM_BUDGET_USD.labels(team=team_name, period="daily").set(daily if daily is not None else 0)
    TEAM_BUDGET_USD.labels(team=team_name, period="monthly").set(
        monthly if monthly is not None else 0
    )


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
