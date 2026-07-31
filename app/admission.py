"""
Priority-aware admission control for outbound provider calls.

A bounded pool of concurrent provider calls ("slots"), with waiters served
in priority order (high before standard before low) and tier-specific max
wait times, so lower-priority traffic degrades gracefully under load
instead of either starving high-priority traffic or queuing forever.

This is deliberately a single-process, in-memory scheduler - not a
distributed queue (Redis, Kafka, a broker, ...). For a local demo, a
distributed broker would be solving a problem this gateway doesn't have
yet (it isn't horizontally scaled): the actual goal is "don't let
low-priority traffic starve high-priority traffic on THIS instance", and a
heap-based priority scheduler does that directly, with far less
operational surface than standing up a message broker. If the gateway is
later scaled to multiple instances, the natural next step is a shared
(e.g. Redis-backed) version of the same idea - noted as a "Later" item,
not built here.

Design:
  - `max_concurrent` outbound provider calls at a time - effectively a
    semaphore.
  - A request that can't get a slot immediately is queued in a min-heap
    keyed by (priority_rank, sequence_number): higher priority always
    jumps ahead of lower priority; same-priority requests stay FIFO.
  - Each priority tier has its own max wait time (config-driven). If a
    waiter's turn hasn't come by then, it's shed with AdmissionTimeout
    rather than being served extremely late - this is the "lower-priority
    degradation" behavior: low-priority traffic fails fast under load
    instead of silently queuing behind an unbounded backlog of
    higher-priority work.
  - Queue position is decided by priority, not by fixed per-tier
    concurrency reservations - so when the system is idle, low-priority
    traffic still gets the full pool; the tiering only matters once
    there's contention.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator

PRIORITY_RANK = {"high": 0, "standard": 1, "low": 2}
DEFAULT_MAX_WAIT_SECONDS = 5.0


class AdmissionTimeout(Exception):
    """Raised when a request waited longer than its tier's max wait time."""

    def __init__(self, priority: str, waited_seconds: float):
        self.priority = priority
        self.waited_seconds = waited_seconds
        super().__init__(
            f"admission timed out for priority='{priority}' after {waited_seconds:.2f}s"
        )


@dataclass(order=True)
class _Waiter:
    sort_key: tuple[int, int] = field(compare=True)
    future: asyncio.Future = field(compare=False)


class AdmissionController:
    def __init__(self, max_concurrent: int, max_wait_seconds: dict[str, float]):
        self._max_concurrent = max_concurrent
        self._max_wait_seconds = max_wait_seconds
        self._in_flight = 0
        self._heap: list[_Waiter] = []
        self._seq = itertools.count()
        self._lock = asyncio.Lock()

    async def _admit_waiters_locked(self) -> None:
        """Caller must hold self._lock. Hands out free slots to queued
        waiters in priority order. Waiters whose future was already
        cancelled (they timed out) are skipped - lazy deletion, cheaper
        than removing them from the middle of the heap up front."""
        while self._in_flight < self._max_concurrent and self._heap:
            waiter = heapq.heappop(self._heap)
            if not waiter.future.done():
                self._in_flight += 1
                waiter.future.set_result(None)

    async def acquire(self, priority: str) -> None:
        """Blocks until a slot is available or the tier's wait budget is
        exhausted. Raises AdmissionTimeout in the latter case."""
        wait_budget = self._max_wait_seconds.get(priority, DEFAULT_MAX_WAIT_SECONDS)
        future: asyncio.Future = asyncio.get_event_loop().create_future()

        async with self._lock:
            if self._in_flight < self._max_concurrent:
                self._in_flight += 1
                future.set_result(None)
            else:
                rank = PRIORITY_RANK.get(priority, PRIORITY_RANK["standard"])
                heapq.heappush(self._heap, _Waiter((rank, next(self._seq)), future))

        started_waiting = time.monotonic()
        try:
            await asyncio.wait_for(future, timeout=wait_budget)
        except asyncio.TimeoutError:
            waited = time.monotonic() - started_waiting
            raise AdmissionTimeout(priority, waited)

    async def release(self) -> None:
        async with self._lock:
            self._in_flight -= 1
            await self._admit_waiters_locked()

    @asynccontextmanager
    async def slot(self, priority: str) -> AsyncIterator[None]:
        """Convenience context manager for the common acquire/try/release
        pattern. The streaming path acquires/releases manually instead,
        since the release has to happen inside the SSE generator's
        `finally`, after the endpoint has already returned the response."""
        await self.acquire(priority)
        try:
            yield
        finally:
            await self.release()


_controller: AdmissionController | None = None


def build_admission_controller(
    max_concurrent: int, max_wait_seconds: dict[str, float]
) -> AdmissionController:
    global _controller
    _controller = AdmissionController(max_concurrent, max_wait_seconds)
    return _controller


def get_admission_controller() -> AdmissionController:
    if _controller is None:
        raise RuntimeError("Admission controller not initialized yet")
    return _controller
