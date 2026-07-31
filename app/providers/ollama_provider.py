from __future__ import annotations

import json
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


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(self, config: ProviderConfig):
        self._base_url = config.base_url

    async def chat_completion(
        self, request: ChatCompletionRequest, provider_model: str
    ) -> ChatCompletionResponse:
        payload = {
            "model": provider_model,
            "messages": [m.model_dump() for m in request.messages],
            "stream": False,
            "options": {},
        }
        if request.temperature is not None:
            payload["options"]["temperature"] = request.temperature

        async with httpx.AsyncClient(base_url=self._base_url, timeout=120.0) as client:
            resp = await client.post("/api/chat", json=payload)

        if resp.status_code >= 400:
            raise ProviderError(
                f"Ollama error: {resp.text}", status_code=resp.status_code
            )

        data = resp.json()
        message = data.get("message", {})

        return ChatCompletionResponse(
            id=f"ollama-{int(time.time())}",
            model=request.model,
            provider=self.name,
            created=int(time.time()),
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=ChatMessage(
                        role="assistant", content=message.get("content", "")
                    ),
                    finish_reason="stop" if data.get("done") else None,
                )
            ],
            usage=Usage(
                prompt_tokens=data.get("prompt_eval_count"),
                completion_tokens=data.get("eval_count"),
                total_tokens=(
                    (data.get("prompt_eval_count") or 0) + (data.get("eval_count") or 0)
                    if data.get("prompt_eval_count") is not None
                    or data.get("eval_count") is not None
                    else None
                ),
            ),
        )

    async def chat_completion_stream(
        self, request: ChatCompletionRequest, provider_model: str
    ) -> AsyncIterator[ChatCompletionChunk]:
        payload = {
            "model": provider_model,
            "messages": [m.model_dump() for m in request.messages],
            "stream": True,
            "options": {},
        }
        if request.temperature is not None:
            payload["options"]["temperature"] = request.temperature

        response_id = f"ollama-stream-{int(time.time())}"
        created = int(time.time())

        async with httpx.AsyncClient(base_url=self._base_url, timeout=120.0) as client:
            async with client.stream("POST", "/api/chat", json=payload) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise ProviderError(
                        f"Ollama error: {body.decode()}", status_code=resp.status_code
                    )

                # Ollama streams newline-delimited JSON, not SSE "data:" lines.
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    message = data.get("message", {})
                    done = data.get("done", False)

                    yield ChatCompletionChunk(
                        id=response_id,
                        model=request.model,
                        provider=self.name,
                        created=created,
                        choices=[
                            ChatCompletionChunkChoice(
                                index=0,
                                delta=ChatCompletionChunkDelta(
                                    content=message.get("content", "")
                                ),
                                finish_reason="stop" if done else None,
                            )
                        ],
                    )

    async def health_check(self, provider_model: str, timeout: float) -> ProbeResult:
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(base_url=self._base_url, timeout=timeout) as client:
                # /api/tags lists locally-pulled models - cheap, no inference.
                resp = await client.get("/api/tags")
            latency_ms = (time.monotonic() - start) * 1000
            if resp.status_code != 200:
                return ProbeResult(
                    success=False, latency_ms=latency_ms, error=f"HTTP {resp.status_code}"
                )
            data = resp.json()
            # Tags look like "llama3:latest" - compare on the base name only.
            local_names = {
                m.get("name", "").split(":")[0] for m in data.get("models", [])
            }
            base_name = provider_model.split(":")[0]
            if base_name in local_names:
                return ProbeResult(success=True, latency_ms=latency_ms)
            return ProbeResult(
                success=False, latency_ms=latency_ms,
                error=f"model '{provider_model}' not pulled locally",
            )
        except httpx.HTTPError as e:
            latency_ms = (time.monotonic() - start) * 1000
            return ProbeResult(success=False, latency_ms=latency_ms, error=str(e))
