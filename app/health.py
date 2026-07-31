"""
Background provider health checks.

Runs a periodic probe against every configured (provider, logical model)
pair using each provider's cheap, token-free health_check() (see
app.providers.base.Provider.health_check), and maintains a rolling window
of latency/success per logical model to derive a healthy/degraded/down
status.

This is deliberately in-memory and per-process (a deque per model), not
persisted/shared via Redis like Steps 5/6's state: health is a
per-instance concern almost by definition - this instance's actual ability
to reach the provider right now. Sharing it across instances via Redis
would actually be *less* correct: a network blip local to one gateway
instance shouldn't be reported as global provider health. If this
gateway is later scaled horizontally, each instance running its own probe
loop against the same providers is the right model, not a shared one -
noted as a non-issue rather than a gap.

Status derivation, per logical model, from its rolling window:
  - "down": consecutive_failures >= config.consecutive_failures_for_down
  - "degraded": not down, but error_rate >= config.degraded_error_rate,
    or avg latency of successful probes >= config.degraded_latency_ms
  - "healthy": otherwise
  - "unknown": no probes have completed yet (e.g. right after startup)

Explicitly NOT in scope here: actually acting on the health status -
routing around an unhealthy provider (fallback) or opening a circuit
breaker to stop calling it - those are later steps. This step only
observes and reports.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass

from app.config import GatewayConfig, HealthCheckConfig
from app.providers.base import ProbeResult

logger = logging.getLogger("llm_gateway.health")


@dataclass
class ModelHealthSnapshot:
    model: str
    provider: str
    status: str  # "healthy" | "degraded" | "down" | "unknown"
    error_rate: float
    avg_latency_ms: float | None
    consecutive_failures: int
    sample_count: int
    last_check_at: float | None
    last_error: str | None


class HealthTracker:
    def __init__(self, config: HealthCheckConfig):
        self._config = config
        self._history: dict[str, deque] = {}  # model -> deque[(ts, success, latency_ms)]
        self._consecutive_failures: dict[str, int] = {}
        self._last_error: dict[str, str | None] = {}
        self._last_check_at: dict[str, float] = {}
        self._provider_of: dict[str, str] = {}

    def record(self, model: str, provider: str, result: ProbeResult) -> None:
        history = self._history.setdefault(model, deque(maxlen=self._config.window_size))
        history.append((time.time(), result.success, result.latency_ms))
        self._provider_of[model] = provider
        self._last_check_at[model] = time.time()
        self._last_error[model] = result.error

        if result.success:
            self._consecutive_failures[model] = 0
        else:
            self._consecutive_failures[model] = self._consecutive_failures.get(model, 0) + 1

    def snapshot(self, model: str) -> ModelHealthSnapshot:
        history = self._history.get(model)
        provider = self._provider_of.get(model, "unknown")

        if not history:
            return ModelHealthSnapshot(
                model=model, provider=provider, status="unknown", error_rate=0.0,
                avg_latency_ms=None, consecutive_failures=0, sample_count=0,
                last_check_at=None, last_error=None,
            )

        successes = [latency for _, ok, latency in history if ok]
        failure_count = sum(1 for _, ok, _ in history if not ok)
        error_rate = failure_count / len(history)
        avg_latency = sum(successes) / len(successes) if successes else None
        consecutive = self._consecutive_failures.get(model, 0)

        if consecutive >= self._config.consecutive_failures_for_down:
            status = "down"
        elif error_rate >= self._config.degraded_error_rate or (
            avg_latency is not None and avg_latency >= self._config.degraded_latency_ms
        ):
            status = "degraded"
        else:
            status = "healthy"

        return ModelHealthSnapshot(
            model=model,
            provider=provider,
            status=status,
            error_rate=error_rate,
            avg_latency_ms=avg_latency,
            consecutive_failures=consecutive,
            sample_count=len(history),
            last_check_at=self._last_check_at.get(model),
            last_error=self._last_error.get(model),
        )

    def all_snapshots(self) -> list[ModelHealthSnapshot]:
        return [self.snapshot(model) for model in self._history]


async def _probe_all_models(config: GatewayConfig, tracker: HealthTracker) -> None:
    # Imported here (not at module load) to avoid a circular import: registry
    # is built after health.py's build_* functions are referenced in main.py.
    from app.providers.registry import get_registry

    registry = get_registry()
    timeout = config.health_check.timeout_seconds

    async def probe_one(model_name: str) -> None:
        route = config.models[model_name]
        provider = registry.get(route.provider)
        if provider is None:
            return
        result = await provider.health_check(route.provider_model, timeout)
        tracker.record(model_name, route.provider, result)
        if not result.success:
            logger.warning(
                "health_probe_failed model=%s provider=%s error=%s",
                model_name, route.provider, result.error,
            )

    await asyncio.gather(*[probe_one(name) for name in config.models], return_exceptions=True)


class HealthProbeLoop:
    """Runs _probe_all_models on a fixed interval until stopped."""

    def __init__(self, config: GatewayConfig, tracker: HealthTracker):
        self._config = config
        self._tracker = tracker
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while True:
            try:
                await _probe_all_models(self._config, self._tracker)
            except Exception:  # noqa: BLE001 - a probe bug must never kill the loop
                logger.exception("health_probe_loop_error")
            await asyncio.sleep(self._config.health_check.interval_seconds)


_tracker: HealthTracker | None = None
_loop: HealthProbeLoop | None = None


def build_health_tracker(config: GatewayConfig) -> HealthTracker:
    global _tracker
    _tracker = HealthTracker(config.health_check)
    return _tracker


def get_health_tracker() -> HealthTracker:
    if _tracker is None:
        raise RuntimeError("Health tracker not initialized yet")
    return _tracker


def start_health_probe_loop(config: GatewayConfig, tracker: HealthTracker) -> HealthProbeLoop:
    global _loop
    _loop = HealthProbeLoop(config, tracker)
    _loop.start()
    return _loop


async def stop_health_probe_loop() -> None:
    if _loop is not None:
        await _loop.stop()
