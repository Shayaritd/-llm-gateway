"""
Shared fixtures for the integration test suite (Step 17).

Uses the same patterns established throughout manual verification in this
project: httpx's ASGITransport to drive the real FastAPI app in-process
(no real network, no uvicorn subprocess needed), with real Redis for
rate-limit/budget/admin state (this project's distributed-correctness
guarantees only mean something when tested against the real Redis Lua
scripts and atomic operations they rely on - a fake Redis would let a real
race-condition bug pass silently) and mock providers standing in for
OpenAI/Anthropic/Ollama so tests are fast, free, and deterministic.

Requires a local Redis reachable at redis://localhost:6379/0 (see
requirements-dev.txt / repo README for how to start one).
"""
from __future__ import annotations

import time
from typing import AsyncIterator

import httpx
import pytest_asyncio
import redis.asyncio as redis

from app.admin_store import build_admin_store
from app.admission import build_admission_controller
from app.audit import build_audit_log
from app.budget import build_budget_tracker
from app.circuit_breaker import build_circuit_breaker
from app.config import load_config
from app.health import build_health_tracker
from app.providers.base import Provider, ProviderError
from app.providers.registry import build_registry, get_registry
from app.ratelimit import build_rate_limiter
from app.schemas import (
    ChatCompletionChoice,
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
    ChatCompletionResponse,
    ChatMessage,
    Usage,
)
from app.tracing import configure_tracing

TEAM_A_KEY = "sk-team-a-demo-key"  # allowed: gpt-4o-mini, claude-3-5-sonnet, llama3; priority high
TEAM_B_KEY = "sk-team-b-demo-key"  # allowed: gpt-4o-mini only; tight rate limit (rpm=5)
TEAM_C_KEY = "sk-team-c-demo-key"  # allowed_providers=[openai] only; unrestricted otherwise
ADMIN_KEY = "sk-admin-demo-key"


class WorksProvider(Provider):
    """Always succeeds, with a small (default 0s) artificial delay."""

    def __init__(self, name: str, delay: float = 0.0):
        self.name = name
        self.delay = delay
        self.call_count = 0

    async def chat_completion(self, request, provider_model):
        import asyncio

        self.call_count += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return ChatCompletionResponse(
            id=f"{self.name}-1", model=request.model, provider=self.name, created=int(time.time()),
            choices=[
                ChatCompletionChoice(
                    index=0, message=ChatMessage(role="assistant", content=f"hello from {self.name}"),
                    finish_reason="stop",
                )
            ],
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    async def chat_completion_stream(self, request, provider_model):
        self.call_count += 1
        yield ChatCompletionChunk(
            id=f"{self.name}-s1", model=request.model, provider=self.name, created=int(time.time()),
            choices=[ChatCompletionChunkChoice(index=0, delta=ChatCompletionChunkDelta(content=f"hi from {self.name}"), finish_reason=None)],
        )
        yield ChatCompletionChunk(
            id=f"{self.name}-s1", model=request.model, provider=self.name, created=int(time.time()),
            choices=[ChatCompletionChunkChoice(index=0, delta=ChatCompletionChunkDelta(), finish_reason="stop")],
        )

    async def health_check(self, provider_model, timeout):
        raise NotImplementedError


class FailsProvider(Provider):
    """Always fails. status_code/retryable control app.retry's classification."""

    def __init__(self, name: str, status_code: int = 503, retryable: bool | None = None):
        self.name = name
        self.status_code = status_code
        self.retryable = retryable
        self.call_count = 0

    async def chat_completion(self, request, provider_model):
        self.call_count += 1
        raise ProviderError(f"{self.name} failing", status_code=self.status_code, retryable=self.retryable)

    async def chat_completion_stream(self, request, provider_model):
        self.call_count += 1
        raise ProviderError(f"{self.name} failing", status_code=self.status_code, retryable=self.retryable)
        yield  # pragma: no cover - makes this a generator function

    async def health_check(self, provider_model, timeout):
        raise NotImplementedError


class FailsMidStreamProvider(Provider):
    """Yields one real chunk, then fails - for testing that fallback is NOT attempted post-first-chunk."""

    def __init__(self, name: str):
        self.name = name
        self.call_count = 0

    async def chat_completion(self, request, provider_model):
        raise NotImplementedError

    async def chat_completion_stream(self, request, provider_model):
        self.call_count += 1
        yield ChatCompletionChunk(
            id=f"{self.name}-mid", model=request.model, provider=self.name, created=int(time.time()),
            choices=[ChatCompletionChunkChoice(index=0, delta=ChatCompletionChunkDelta(content="partial "), finish_reason=None)],
        )
        raise ProviderError(f"{self.name} dropped mid-stream", status_code=503)

    async def health_check(self, provider_model, timeout):
        raise NotImplementedError


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[redis.Redis]:
    client = redis.from_url("redis://localhost:6379/0", decode_responses=True)
    await client.flushall()
    yield client
    await client.flushall()
    await client.aclose()


@pytest_asyncio.fixture
async def gateway(redis_client) -> AsyncIterator[httpx.AsyncClient]:
    """Fully wired app (real Redis, real config.yaml, mock providers by
    default) exposed as an httpx client via ASGI transport - no real
    network, no subprocess."""
    config = load_config()
    configure_tracing()
    build_registry(config)
    build_rate_limiter(config.redis.url)
    build_budget_tracker(config.redis.url)
    build_admission_controller(
        config.admission.max_concurrent_requests, config.admission.max_wait_seconds
    )
    build_health_tracker(config)  # probe loop intentionally not started - not needed for these tests
    build_admin_store(config.redis.url)
    build_audit_log(config.redis.url)
    build_circuit_breaker(config.circuit_breaker)

    # Default: every provider works, so tests that don't care about
    # provider behavior aren't flaky. Tests that DO care replace entries
    # in get_registry()._providers directly (see individual test files).
    registry = get_registry()
    registry._providers["openai"] = WorksProvider("openai")
    registry._providers["anthropic"] = WorksProvider("anthropic")
    registry._providers["ollama"] = WorksProvider("ollama")

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def auth_headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def chat_payload(model: str = "gpt-4o-mini", content: str = "hi", stream: bool = False) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": content}], "stream": stream}
