"""
Config loader skeleton.

Loads config/config.yaml into typed models once at startup and exposes
a singleton accessor. Hot reload is NOT implemented in this step
(see "Later" note in the project README) - this only loads once.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "config.yaml"

Priority = Literal["high", "standard", "low"]


class TeamPolicy(BaseModel):
    """Config-driven request enrichment / policy rules for a team.

    All fields are optional - a team with no policy section behaves exactly
    as before (no injection, no disclaimer, no filtering).
    """

    # Prepended as (or merged into) a system message before dispatch.
    system_prompt: str | None = None
    # Appended to the assistant's final content, for both streaming and
    # non-streaming responses.
    disclaimer: str | None = None
    # Case-insensitive substrings; a match in any message rejects the
    # request before it reaches the provider. Simple keyword check, not a
    # moderation model/API call.
    banned_keywords: list[str] = Field(default_factory=list)


class RateLimitConfig(BaseModel):
    """Per-team token bucket limits. Both are enforced independently."""

    rpm: int  # requests per minute (bucket capacity; refills continuously)
    tpm: int  # tokens per minute (bucket capacity; refills continuously)


class BudgetConfig(BaseModel):
    """Per-team spend caps in USD. Either may be omitted (no cap on that period)."""

    daily_limit_usd: float | None = None
    monthly_limit_usd: float | None = None


class TeamConfig(BaseModel):
    name: str
    api_key: str
    allowed_models: list[str]
    # If None, the team is not restricted at the provider level (only by
    # allowed_models). If set, this is an additional, independent check -
    # a model can be in allowed_models yet still be blocked if its
    # provider isn't in allowed_providers.
    allowed_providers: list[str] | None = None
    policy: TeamPolicy = Field(default_factory=TeamPolicy)
    # None = unlimited (not recommended for real deployments, but useful
    # for local demo teams that shouldn't be throttled).
    rate_limit: RateLimitConfig | None = None
    budget: BudgetConfig | None = None
    # Config-driven priority policy: which tier this team's traffic gets
    # when the gateway is at capacity. See app.admission.
    priority: Priority = "standard"


class ModelRoute(BaseModel):
    provider: str
    provider_model: str
    # Ordered list of other logical model names to try, in order, if this
    # model's provider call ultimately fails (after its retry policy is
    # exhausted). Each entry acts as a lower/alternate tier: a team's
    # request effectively "degrades" through this chain rather than
    # failing outright. See app/fallback.py.
    fallback_chain: list[str] = Field(default_factory=list)


class ProviderConfig(BaseModel):
    base_url: str
    api_key_env: str | None = None


class RedisConfig(BaseModel):
    url: str = "redis://localhost:6379/0"


class ModelPricing(BaseModel):
    """USD cost per 1,000 tokens, keyed by logical model name in config.yaml."""

    input_cost_per_1k: float
    output_cost_per_1k: float


class RetryPolicyConfig(BaseModel):
    """See app/retry.py for the retry behavior this drives.

    max_retries counts additional attempts beyond the first - "up to 3
    retries" means at most 4 total attempts against a given candidate.
    Only retryable failures (timeouts, rate limiting, transient upstream
    errors) consume this budget; non-retryable ones (auth, invalid
    request) fail on the first attempt.
    """

    max_retries: int = 3
    backoff_base_seconds: float = 0.25
    backoff_max_seconds: float = 4.0


class AdmissionConfig(BaseModel):
    """Bounded concurrency + priority scheduling for outbound provider calls.

    See app.admission for the scheduling behavior this drives.
    """

    max_concurrent_requests: int = 10
    # How long a request may wait for a free slot before being shed with a
    # 503, per priority tier. Lower tiers get shorter budgets - that's the
    # "lower-priority degradation" behavior: under load, low-priority
    # traffic fails fast instead of queuing indefinitely behind higher tiers.
    max_wait_seconds: dict[str, float] = Field(
        default_factory=lambda: {"high": 30.0, "standard": 10.0, "low": 2.0}
    )


class AdminConfig(BaseModel):
    """A named admin identity for the Admin API (app/routers/admin.py).

    Deliberately separate from TeamConfig - an admin key is not a team key
    and shouldn't authenticate to either surface. `name` is used to
    attribute audit log entries to who made a change.
    """

    name: str
    api_key: str


class HealthCheckConfig(BaseModel):
    """Tuning for the background provider health probes (see app/health.py)."""

    interval_seconds: float = 30.0
    timeout_seconds: float = 5.0
    window_size: int = 20  # rolling history length per model
    consecutive_failures_for_down: int = 3
    degraded_error_rate: float = 0.2  # fraction of window that may fail before "degraded"
    degraded_latency_ms: float = 3000.0


class CircuitBreakerConfig(BaseModel):
    """Tuning for per-model circuit breakers (see app/circuit_breaker.py)."""

    failure_threshold: int = 3  # consecutive real-request failures (CLOSED) that trips to OPEN
    cooldown_seconds: float = 10.0  # how long OPEN lasts before a HALF_OPEN trial is allowed
    half_open_max_trials: int = 1  # concurrent trial requests allowed while HALF_OPEN


class GatewayConfig(BaseModel):
    teams: list[TeamConfig]
    models: dict[str, ModelRoute]
    providers: dict[str, ProviderConfig]
    redis: RedisConfig = Field(default_factory=RedisConfig)
    pricing: dict[str, ModelPricing] = Field(default_factory=dict)
    admission: AdmissionConfig = Field(default_factory=AdmissionConfig)
    admins: list[AdminConfig] = Field(default_factory=list)
    health_check: HealthCheckConfig = Field(default_factory=HealthCheckConfig)
    retry_policy: RetryPolicyConfig = Field(default_factory=RetryPolicyConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)

    def get_team_by_api_key(self, api_key: str) -> TeamConfig | None:
        for team in self.teams:
            if team.api_key == api_key:
                return team
        return None

    def get_admin_by_api_key(self, api_key: str) -> AdminConfig | None:
        for admin in self.admins:
            if admin.api_key == api_key:
                return admin
        return None


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> GatewayConfig:
    with open(path, "r") as f:
        raw = yaml.safe_load(f)
    return GatewayConfig(**raw)


@lru_cache(maxsize=1)
def get_config() -> GatewayConfig:
    """Process-wide singleton. Cleared implicitly on process restart only."""
    return load_config()
