"""
Admin API: view and update per-team rate limits/budgets/priority, inspect
usage/spend, and an audit trail of who changed what and when.

Auth is a separate, admin-only API key (app.config.AdminConfig), checked by
require_admin below - intentionally NOT TeamAuthMiddleware, since admin
identities aren't teams (see app/middleware/auth.py, which exempts /admin/*
entirely so this dependency is the only gate here).

Limits/budgets/priority set here are runtime *overrides* (app.admin_store)
stored in Redis, layered on top of the static config.yaml team definitions,
which remain the source of truth for auth, model access, and policy. This
means changes take effect immediately across every gateway process sharing
the same Redis, and survive a process restart.

PATCH semantics: only fields present in the request body are treated as
override intents (via Pydantic's exclude_unset), so omitting a field
leaves any existing override for it untouched, while explicitly sending
`null` sets that field to "no limit" - a deliberate admin choice, distinct
from DELETE, which clears overrides entirely and reverts to config.yaml.
Nested objects (rate_limit, budget) are replaced as a whole, not deep-merged
field-by-field - a normal, well-understood PATCH tradeoff worth being
explicit about rather than adding partial-nested-merge complexity for a
local-demo admin API.

Explicitly NOT in scope: a frontend admin UI, Grafana dashboards, resetting
already-recorded spend, or RBAC/SSO for admin identities - a single named,
shared-secret admin identity is the "simple but production-minded" baseline
here; a real deployment would put a proper identity provider in front of
this instead of a flat API key list.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from app.admin_store import AdminStore, get_admin_store
from app.audit import get_audit_log
from app.budget import get_budget_tracker
from app.config import (
    BudgetConfig,
    GatewayConfig,
    Priority,
    RateLimitConfig,
    TeamConfig,
    TeamPolicy,
    get_config,
)
from app.http_auth import extract_api_key
from app.metrics import set_team_budget_gauges
from app.ratelimit import get_rate_limiter

router = APIRouter(prefix="/admin", tags=["admin"])


async def require_admin(request: Request) -> str:
    """Returns the authenticated admin's name (used for audit attribution)."""
    api_key = extract_api_key(request, fallback_header="x-admin-key")
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing admin API key")

    admin = get_config().get_admin_by_api_key(api_key)
    if admin is None:
        raise HTTPException(status_code=401, detail="Invalid admin API key")

    return admin.name


class TeamLimitsView(BaseModel):
    name: str
    allowed_models: list[str]
    allowed_providers: list[str] | None
    policy: TeamPolicy
    rate_limit: RateLimitConfig | None
    budget: BudgetConfig | None
    priority: Priority
    overridden: bool  # whether any admin override is currently applied


class TeamUsageView(BaseModel):
    name: str
    daily_spend_usd: float
    monthly_spend_usd: float
    daily_limit_usd: float | None
    monthly_limit_usd: float | None
    daily_pct_of_cap: float | None
    monthly_pct_of_cap: float | None
    rpm_remaining: float | None
    tpm_remaining: float | None


class TeamLimitsUpdate(BaseModel):
    rate_limit: RateLimitConfig | None = None
    budget: BudgetConfig | None = None
    priority: Priority | None = None


class AuditLogEntry(BaseModel):
    timestamp: float
    admin: str
    action: str
    team: str
    before: dict | None = None
    after: dict | None = None


def _find_static_team(config: GatewayConfig, team_name: str) -> TeamConfig:
    for team in config.teams:
        if team.name == team_name:
            return team
    raise HTTPException(status_code=404, detail=f"Unknown team '{team_name}'")


async def _to_view(team_name: str, config: GatewayConfig, store: AdminStore) -> TeamLimitsView:
    static_team = _find_static_team(config, team_name)
    effective = await store.effective_team(static_team)
    overridden = bool(await store.get_overrides(team_name))
    return TeamLimitsView(
        name=effective.name,
        allowed_models=effective.allowed_models,
        allowed_providers=effective.allowed_providers,
        policy=effective.policy,
        rate_limit=effective.rate_limit,
        budget=effective.budget,
        priority=effective.priority,
        overridden=overridden,
    )


@router.get("/teams", response_model=list[TeamLimitsView])
async def list_teams(admin_name: str = Depends(require_admin)):
    config = get_config()
    store = get_admin_store()
    return [await _to_view(t.name, config, store) for t in config.teams]


@router.get("/teams/{team_name}", response_model=TeamLimitsView)
async def get_team(team_name: str, admin_name: str = Depends(require_admin)):
    config = get_config()
    store = get_admin_store()
    return await _to_view(team_name, config, store)


@router.patch("/teams/{team_name}/limits", response_model=TeamLimitsView)
async def update_team_limits(
    team_name: str,
    update: TeamLimitsUpdate,
    admin_name: str = Depends(require_admin),
):
    config = get_config()
    _find_static_team(config, team_name)  # 404s if the team doesn't exist at all

    store = get_admin_store()
    audit = get_audit_log()

    before = await store.get_overrides(team_name)
    overrides = dict(before)

    # Only fields the caller actually included in the JSON body count as an
    # override intent - exclude_unset distinguishes "field omitted" from
    # "field explicitly set to null", which matters here (see module docstring).
    provided = update.model_dump(exclude_unset=True)
    overrides.update(provided)

    await store.set_overrides(team_name, overrides)
    await audit.record(
        admin_name=admin_name,
        action="update_team_limits",
        team_name=team_name,
        before=before,
        after=overrides,
    )

    view = await _to_view(team_name, config, store)
    set_team_budget_gauges(team_name, view.budget)  # keep the alerting metric in sync immediately
    return view


@router.delete("/teams/{team_name}/limits", response_model=TeamLimitsView)
async def clear_team_overrides(team_name: str, admin_name: str = Depends(require_admin)):
    config = get_config()
    _find_static_team(config, team_name)

    store = get_admin_store()
    audit = get_audit_log()

    before = await store.get_overrides(team_name)
    await store.clear_overrides(team_name)
    await audit.record(
        admin_name=admin_name,
        action="clear_team_overrides",
        team_name=team_name,
        before=before,
        after={},
    )

    view = await _to_view(team_name, config, store)
    set_team_budget_gauges(team_name, view.budget)
    return view


@router.get("/teams/{team_name}/usage", response_model=TeamUsageView)
async def get_team_usage(team_name: str, admin_name: str = Depends(require_admin)):
    config = get_config()
    store = get_admin_store()
    static_team = _find_static_team(config, team_name)
    effective = await store.effective_team(static_team)

    budget_tracker = get_budget_tracker()
    daily, monthly = await budget_tracker.get_spend(team_name)

    daily_limit = effective.budget.daily_limit_usd if effective.budget else None
    monthly_limit = effective.budget.monthly_limit_usd if effective.budget else None

    rpm_remaining = tpm_remaining = None
    if effective.rate_limit is not None:
        limiter = get_rate_limiter()
        rpm_remaining, tpm_remaining = await limiter.peek(team_name, effective.rate_limit)

    return TeamUsageView(
        name=team_name,
        daily_spend_usd=daily,
        monthly_spend_usd=monthly,
        daily_limit_usd=daily_limit,
        monthly_limit_usd=monthly_limit,
        daily_pct_of_cap=(100 * daily / daily_limit) if daily_limit else None,
        monthly_pct_of_cap=(100 * monthly / monthly_limit) if monthly_limit else None,
        rpm_remaining=rpm_remaining,
        tpm_remaining=tpm_remaining,
    )


@router.get("/audit-log", response_model=list[AuditLogEntry])
async def get_audit_log_entries(
    team: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=1000),
    admin_name: str = Depends(require_admin),
):
    audit = get_audit_log()
    return await audit.list_recent(limit=limit, team_name=team)
