"""
Normalized request/response schema, shared by every provider adapter.

Every provider adapter must translate:
  ChatCompletionRequest  -> provider-specific request payload
  provider-specific response -> ChatCompletionResponse

This keeps routers and clients provider-agnostic.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False


class Usage(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class ChatCompletionChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: str | None = None


class ChatCompletionResponse(BaseModel):
    id: str
    model: str  # the model that actually served this request (may differ from what was requested - see requested_model)
    provider: str  # the provider that actually served this request
    created: int
    choices: list[ChatCompletionChoice]
    usage: Usage = Field(default_factory=Usage)
    # Set only when fallback routing (Step 10) served a different model than
    # what the client asked for - i.e. `model` above is a fallback, not the
    # original request. None means no fallback occurred.
    requested_model: str | None = None
    # Brief record of candidates that failed (after their retry budget was
    # exhausted) before the one that actually served this request. None or
    # empty if the first candidate succeeded outright.
    fallback_attempts: list[dict] | None = None


class ChatCompletionChunkDelta(BaseModel):
    """Only the incremental piece produced by this chunk - never the full text so far."""

    role: Literal["assistant"] | None = None
    content: str | None = None


class ChatCompletionChunkChoice(BaseModel):
    index: int
    delta: ChatCompletionChunkDelta
    finish_reason: str | None = None


class ChatCompletionChunk(BaseModel):
    """Normalized streaming chunk. Every provider's raw SSE/NDJSON event is
    translated into this shape before being forwarded to the client, so the
    client sees one consistent streaming format regardless of provider."""

    id: str
    model: str  # the model that actually served this stream (see requested_model)
    provider: str
    created: int
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    choices: list[ChatCompletionChunkChoice]
    requested_model: str | None = None
