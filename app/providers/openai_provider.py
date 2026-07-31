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


class OpenAIProvider(Provider):
    name = "openai"

    def __init__(self, config: ProviderConfig):
        self._base_url = config.base_url
        self._api_key = os.environ.get(config.api_key_env or "", "")

    async def chat_completion(
        self, request: ChatCompletionRequest, provider_model: str
    ) -> ChatCompletionResponse:
        if not self._api_key:
            raise ProviderError(
                "OpenAI API key not configured (set OPENAI_API_KEY)",
                status_code=500,
                retryable=False,
            )
        payload = {
            "model": provider_model,
            "messages": [m.model_dump() for m in request.messages],
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        headers = {"Authorization": f"Bearer {self._api_key}"}

        async with httpx.AsyncClient(base_url=self._base_url, timeout=60.0) as client:
            resp = await client.post(
                "/chat/completions", json=payload, headers=headers
            )

        if resp.status_code >= 400:
            raise ProviderError(
                f"OpenAI error: {resp.text}", status_code=resp.status_code
            )

        data = resp.json()
        choices = [
            ChatCompletionChoice(
                index=c["index"],
                message=ChatMessage(
                    role=c["message"]["role"], content=c["message"]["content"]
                ),
                finish_reason=c.get("finish_reason"),
            )
            for c in data["choices"]
        ]
        usage = data.get("usage", {})

        return ChatCompletionResponse(
            id=data.get("id", "openai-unknown"),
            model=request.model,
            provider=self.name,
            created=data.get("created", int(time.time())),
            choices=choices,
            usage=Usage(
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"),
            ),
        )

    async def chat_completion_stream(
        self, request: ChatCompletionRequest, provider_model: str
    ) -> AsyncIterator[ChatCompletionChunk]:
        if not self._api_key:
            raise ProviderError(
                "OpenAI API key not configured (set OPENAI_API_KEY)",
                status_code=500,
                retryable=False,
            )
        payload = {
            "model": provider_model,
            "messages": [m.model_dump() for m in request.messages],
            "stream": True,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        headers = {"Authorization": f"Bearer {self._api_key}"}
        response_id = f"openai-stream-{int(time.time())}"
        created = int(time.time())

        async with httpx.AsyncClient(base_url=self._base_url, timeout=60.0) as client:
            async with client.stream(
                "POST", "/chat/completions", json=payload, headers=headers
            ) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise ProviderError(
                        f"OpenAI error: {body.decode()}", status_code=resp.status_code
                    )

                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[len("data:") :].strip()
                    if data_str == "[DONE]":
                        break

                    data = json.loads(data_str)
                    response_id = data.get("id", response_id)
                    created = data.get("created", created)

                    for c in data.get("choices", []):
                        delta = c.get("delta", {})
                        yield ChatCompletionChunk(
                            id=response_id,
                            model=request.model,
                            provider=self.name,
                            created=created,
                            choices=[
                                ChatCompletionChunkChoice(
                                    index=c.get("index", 0),
                                    delta=ChatCompletionChunkDelta(
                                        role=delta.get("role"),
                                        content=delta.get("content"),
                                    ),
                                    finish_reason=c.get("finish_reason"),
                                )
                            ],
                        )

    async def health_check(self, provider_model: str, timeout: float) -> ProbeResult:
        if not self._api_key:
            return ProbeResult(success=False, latency_ms=0.0, error="API key not configured")

        headers = {"Authorization": f"Bearer {self._api_key}"}
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
