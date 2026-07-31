"""
Request routing rules: given a team and a requested logical model name,
decide whether the request is allowed and which provider/provider_model
to dispatch to.

Kept separate from the HTTP layer (routers/chat.py) so the authorization
rules can be reasoned about and unit tested independently of FastAPI.

Two independent, config-driven restrictions apply, in this order:
  1. allowed_models  - the logical model must be in the team's allowlist.
  2. allowed_providers - if set, the model's underlying provider must also
     be in the team's allowlist, even if the model itself was permitted.

Explicitly NOT in scope here: rate limiting, budgets, fallback across
providers - those are later steps.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import GatewayConfig, TeamConfig


class RoutingError(Exception):
    """Raised when a request cannot be routed. Carries an HTTP status code."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class ResolvedRoute:
    provider: str
    provider_model: str


def resolve_route(team: TeamConfig, model: str, config: GatewayConfig) -> ResolvedRoute:
    model_route = config.models.get(model)
    if model_route is None:
        raise RoutingError(f"Unknown model '{model}'", status_code=404)

    if model not in team.allowed_models:
        raise RoutingError(
            f"Team '{team.name}' is not allowed to use model '{model}'",
            status_code=403,
        )

    if (
        team.allowed_providers is not None
        and model_route.provider not in team.allowed_providers
    ):
        raise RoutingError(
            f"Team '{team.name}' is not allowed to use provider "
            f"'{model_route.provider}'",
            status_code=403,
        )

    return ResolvedRoute(
        provider=model_route.provider, provider_model=model_route.provider_model
    )
