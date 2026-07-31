"""
Provider interface. Every backend (OpenAI, Anthropic, Ollama, ...) implements
this so the router never needs to know provider-specific details.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator

from app.schemas import ChatCompletionChunk, ChatCompletionRequest, ChatCompletionResponse


@dataclass
class ProbeResult:
    """Outcome of a single health_check() call. Used by app.health to build
    rolling per-model history - see that module for the healthy/degraded/down
    status derivation."""

    success: bool
    latency_ms: float
    error: str | None = None


class Provider(ABC):
    name: str

    @abstractmethod
    async def chat_completion(
        self, request: ChatCompletionRequest, provider_model: str
    ) -> ChatCompletionResponse:
        """
        Execute a non-streaming chat completion call against the concrete
        provider and return a normalized ChatCompletionResponse.

        `provider_model` is the provider-specific model id resolved from
        config (e.g. "gpt-4o-mini", "claude-3-5-sonnet-20241022", "llama3"),
        as opposed to `request.model`, which is the gateway's logical model
        name.
        """
        raise NotImplementedError

    @abstractmethod
    def chat_completion_stream(
        self, request: ChatCompletionRequest, provider_model: str
    ) -> AsyncIterator[ChatCompletionChunk]:
        """
        Execute a streaming chat completion call and yield normalized
        ChatCompletionChunk objects as they arrive. Implementations parse
        their provider's native stream format (SSE, NDJSON, ...) and
        translate each event into the shared chunk schema.
        """
        raise NotImplementedError

    @abstractmethod
    async def health_check(self, provider_model: str, timeout: float) -> ProbeResult:
        """
        Perform a cheap, token-free liveness probe for a specific
        provider_model (e.g. a model-metadata lookup), and report success,
        latency, and an error message on failure. Never raises - a
        connection failure, timeout, or missing credential is reported as
        ProbeResult(success=False, ...), not an exception, since this is
        called unattended from a background loop (app.health).
        """
        raise NotImplementedError


class ProviderError(Exception):
    """Raised when a provider call fails. Carries the upstream status code
    and a retryable classification used by app.retry.

    By default, retryable is inferred from status_code (timeouts, rate
    limiting, and transient 5xx errors are retryable; invalid-request and
    auth errors are not) - but a raise site can always pass an explicit
    value when it knows better than the status code alone, e.g. a locally
    detected "API key not configured" is reported as a 500 for HTTP
    purposes but is emphatically not retryable (retrying won't make a
    missing credential appear).
    """

    RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}

    def __init__(self, message: str, status_code: int = 502, retryable: bool | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = (
            retryable if retryable is not None else status_code in self.RETRYABLE_STATUS_CODES
        )
