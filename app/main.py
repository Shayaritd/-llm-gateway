from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.admin_store import build_admin_store, get_admin_store
from app.admission import build_admission_controller
from app.audit import build_audit_log, get_audit_log
from app.budget import build_budget_tracker, get_budget_tracker
from app.circuit_breaker import build_circuit_breaker
from app.config import get_config
from app.health import build_health_tracker, start_health_probe_loop, stop_health_probe_loop
from app.middleware.auth import TeamAuthMiddleware
from app.providers.registry import build_registry
from app.ratelimit import build_rate_limiter, get_rate_limiter
from app.routers import admin, chat, health, metrics, provider_health
from app.tracing import configure_tracing, shutdown_tracing

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_tracing()
    config = get_config()
    build_registry(config)
    build_rate_limiter(config.redis.url)
    build_budget_tracker(config.redis.url)
    build_admission_controller(
        config.admission.max_concurrent_requests, config.admission.max_wait_seconds
    )
    health_tracker = build_health_tracker(config)
    start_health_probe_loop(config, health_tracker)
    build_admin_store(config.redis.url)
    build_audit_log(config.redis.url)
    build_circuit_breaker(config.circuit_breaker)
    yield
    await stop_health_probe_loop()
    await get_rate_limiter().close()
    await get_budget_tracker().close()
    await get_admin_store().close()
    await get_audit_log().close()
    shutdown_tracing()


app = FastAPI(title="LLM Gateway", version="0.1.0", lifespan=lifespan)

app.add_middleware(TeamAuthMiddleware)

app.include_router(health.router)
app.include_router(provider_health.router)
app.include_router(metrics.router)
app.include_router(chat.router)
app.include_router(admin.router)
