from __future__ import annotations

import json
import os
import time
from typing import AsyncIterator

import httpx

from app.config import ProviderConfig
from app.providers.base import ProbeResult, Provider, ProviderError
from app.schemas import (
    ChatCompletionChoice,
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Usage,
)

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, config: ProviderConfig):
        self._base_url = config.base_url
        self._api_key = os.environ.get(config.api_key_env or "", "")

    async def chat_completion(
        self, request: ChatCompletionRequest, provider_model: str
    ) -> ChatCompletionResponse:
        if not self._api_key:
            raise ProviderError(
                "Anthropic API key not configured (set ANTHROPIC_API_KEY)",
                status_code=500,
                retryable=False,
            )
        # Anthropic separates the "system" message from the message list.
        system_prompt = None
        messages = []
        for m in request.messages:
            if m.role == "system":
                system_prompt = m.content
            else:
                messages.append({"role": m.role, "content": m.content})

        payload = {
            "model": provider_model,
            "messages": messages,
            "max_tokens": request.max_tokens or 1024,
        }
        if system_prompt:
            payload["system"] = system_prompt
        if request.temperature is not None:
            payload["temperature"] = request.temperature

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }

        async with httpx.AsyncClient(base_url=self._base_url, timeout=60.0) as client:
            resp = await client.post("/messages", json=payload, headers=headers)

        if resp.status_code >= 400:
            raise ProviderError(
                f"Anthropic error: {resp.text}", status_code=resp.status_code
            )

        data = resp.json()
        text = "".join(
            block.get("text", "") for block in data.get("content", [])
        )
        usage = data.get("usage", {})
        prompt_tokens = usage.get("input_tokens")
        completion_tokens = usage.get("output_tokens")
        total_tokens = (
            prompt_tokens + completion_tokens
            if prompt_tokens is not None and completion_tokens is not None
            else None
        )

        return ChatCompletionResponse(
            id=data.get("id", "anthropic-unknown"),
            model=request.model,
            provider=self.name,
            created=int(time.time()),
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content=text),
                    finish_reason=data.get("stop_reason"),
                )
            ],
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            ),
        )

    async def chat_completion_stream(
        self, request: ChatCompletionRequest, provider_model: str
    ) -> AsyncIterator[ChatCompletionChunk]:
        if not self._api_key:
            raise ProviderError(
                "Anthropic API key not configured (set ANTHROPIC_API_KEY)",
                status_code=500,
                retryable=False,
            )

        system_prompt = None
        messages = []
        for m in request.messages:
            if m.role == "system":
                system_prompt = m.content
            else:
                messages.append({"role": m.role, "content": m.content})

        payload = {
            "model": provider_model,
            "messages": messages,
            "max_tokens": request.max_tokens or 1024,
            "stream": True,
        }
        if system_prompt:
            payload["system"] = system_prompt
        if request.temperature is not None:
            payload["temperature"] = request.temperature

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        response_id = f"anthropic-stream-{int(time.time())}"
        created = int(time.time())

        async with httpx.AsyncClient(base_url=self._base_url, timeout=60.0) as client:
            async with client.stream(
                "POST", "/messages", json=payload, headers=headers
            ) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise ProviderError(
                        f"Anthropic error: {body.decode()}",
                        status_code=resp.status_code,
                    )

                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = json.loads(line[len("data:") :].strip())
                    event_type = data.get("type")

                    if event_type == "message_start":
                        response_id = data.get("message", {}).get("id", response_id)

                    elif event_type == "content_block_delta":
                        delta_text = data.get("delta", {}).get("text", "")
                        yield ChatCompletionChunk(
                            id=response_id,
                            model=request.model,
                            provider=self.name,
                            created=created,
                            choices=[
                                ChatCompletionChunkChoice(
                                    index=0,
                                    delta=ChatCompletionChunkDelta(content=delta_text),
                                    finish_reason=None,
                                )
                            ],
                        )

                    elif event_type == "message_delta":
                        stop_reason = data.get("delta", {}).get("stop_reason")
                        yield ChatCompletionChunk(
                            id=response_id,
                            model=request.model,
                            provider=self.name,
                            created=created,
                            choices=[
                                ChatCompletionChunkChoice(
                                    index=0,
                                    delta=ChatCompletionChunkDelta(),
                                    finish_reason=stop_reason,
                                )
                            ],
                        )

    async def health_check(self, provider_model: str, timeout: float) -> ProbeResult:
        if not self._api_key:
            return ProbeResult(success=False, latency_ms=0.0, error="API key not configured")

        headers = {"x-api-key": self._api_key, "anthropic-version": ANTHROPIC_VERSION}
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(base_url=self._base_url, timeout=timeout) as client:
                # GET /models/{id} returns model metadata - no tokens consumed,
                # and 404 specifically tells us this model id isn't available.
                resp = await client.get(f"/models/{provider_model}", headers=headers)
            latency_ms = (time.monotonic() - start) * 1000
            if resp.status_code == 200:
                return ProbeResult(success=True, latency_ms=latency_ms)
            if resp.status_code == 404:
                return ProbeResult(
                    success=False, latency_ms=latency_ms,
                    error=f"model '{provider_model}' not found",
                )
            return ProbeResult(
                success=False, latency_ms=latency_ms, error=f"HTTP {resp.status_code}"
            )
        except httpx.HTTPError as e:
            latency_ms = (time.monotonic() - start) * 1000
            return ProbeResult(success=False, latency_ms=latency_ms, error=str(e))
