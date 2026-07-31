"""
Read-only view of provider-model health and circuit breaker state.
Kept separate from the general /health liveness endpoint since these
report on upstream providers, not on this gateway process itself.

Health (app.health) is passive background probing; circuit breaker state
(app.circuit_breaker) is driven by real request outcomes - see
app/circuit_breaker.py for why these stay separate.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.circuit_breaker import get_circuit_breaker
from app.health import get_health_tracker

router = APIRouter()


class ModelHealthView(BaseModel):
    model: str
    provider: str
    status: str
    error_rate: float
    avg_latency_ms: float | None
    consecutive_failures: int
    sample_count: int
    last_check_at: float | None
    last_error: str | None


class CircuitBreakerView(BaseModel):
    key: str
    state: str
    consecutive_failures: int
    opened_at: float | None
    last_transition_at: float


@router.get("/health/providers", response_model=list[ModelHealthView])
async def provider_health() -> list[ModelHealthView]:
    tracker = get_health_tracker()
    return [ModelHealthView(**snap.__dict__) for snap in tracker.all_snapshots()]


@router.get("/health/circuit-breakers", response_model=list[CircuitBreakerView])
async def circuit_breaker_status() -> list[CircuitBreakerView]:
    breaker = get_circuit_breaker()
    return [CircuitBreakerView(**snap) for snap in breaker.all_snapshots()]
