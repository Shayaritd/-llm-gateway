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


class GeminiProvider(Provider):
    name = "gemini"

    def __init__(self, config: ProviderConfig):
        self._base_url = config.base_url.rstrip("/")
        # Work with config env var name, fallback to standard GEMINI_API_KEY
        env_var = config.api_key_env or "GEMINI_API_KEY"
        self._api_key = os.environ.get(env_var, "") or os.environ.get("GEMINI_API_KEY", "")

    async def chat_completion(
        self, request: ChatCompletionRequest, provider_model: str
    ) -> ChatCompletionResponse:
        if not self._api_key:
            raise ProviderError(
                "Gemini API key not configured (set GEMINI_API_KEY)",
                status_code=500,
                retryable=False,
            )

        # Gemini separates the "system" message from the message list.
        system_instruction = None
        contents = []
        for m in request.messages:
            if m.role == "system":
                system_instruction = {
                    "parts": [{"text": m.content}]
                }
            else:
                role = "user" if m.role == "user" else "model"
                contents.append({
                    "role": role,
                    "parts": [{"text": m.content}]
                })

        payload = {
            "contents": contents
        }
        if system_instruction:
            payload["systemInstruction"] = system_instruction

        generation_config = {}
        if request.temperature is not None:
            generation_config["temperature"] = request.temperature
        if request.max_tokens is not None:
            generation_config["maxOutputTokens"] = request.max_tokens
        if generation_config:
            payload["generationConfig"] = generation_config

        url = f"{self._base_url}/models/{provider_model}:generateContent?key={self._api_key}"

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=payload)

        if resp.status_code >= 400:
            raise ProviderError(
                f"Gemini error: {resp.text}", status_code=resp.status_code
            )

        data = resp.json()
        candidates = data.get("candidates", [])
        choices = []
        for i, c in enumerate(candidates):
            content_data = c.get("content", {})
            parts = content_data.get("parts", [])
            text = "".join(p.get("text", "") for p in parts) if parts else ""
            
            finish_reason = c.get("finishReason")
            if finish_reason:
                finish_reason = finish_reason.lower()

            choices.append(
                ChatCompletionChoice(
                    index=c.get("index", i),
                    message=ChatMessage(
                        role="assistant", content=text
                    ),
                    finish_reason=finish_reason,
                )
            )

        if not choices:
            choices = [
                ChatCompletionChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content=""),
                    finish_reason="stop",
                )
            ]

        usage_data = data.get("usageMetadata", {})
        prompt_tokens = usage_data.get("promptTokenCount")
        completion_tokens = usage_data.get("candidatesTokenCount")
        total_tokens = usage_data.get("totalTokenCount")

        # Fallback to estimation if usage details are missing
        if prompt_tokens is None or completion_tokens is None:
            from app.tokens import estimate_prompt_tokens, estimate_text_tokens
            prompt_tokens = prompt_tokens or estimate_prompt_tokens(request)
            text_content = "".join(choice.message.content for choice in choices)
            completion_tokens = completion_tokens or estimate_text_tokens(text_content)
            total_tokens = total_tokens or (prompt_tokens + completion_tokens)

        return ChatCompletionResponse(
            id=data.get("id", f"gemini-{int(time.time())}"),
            model=request.model,
            provider=self.name,
            created=int(time.time()),
            choices=choices,
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
                "Gemini API key not configured (set GEMINI_API_KEY)",
                status_code=500,
                retryable=False,
            )

        system_instruction = None
        contents = []
        for m in request.messages:
            if m.role == "system":
                system_instruction = {
                    "parts": [{"text": m.content}]
                }
            else:
                role = "user" if m.role == "user" else "model"
                contents.append({
                    "role": role,
                    "parts": [{"text": m.content}]
                })

        payload = {
            "contents": contents
        }
        if system_instruction:
            payload["systemInstruction"] = system_instruction

        generation_config = {}
        if request.temperature is not None:
            generation_config["temperature"] = request.temperature
        if request.max_tokens is not None:
            generation_config["maxOutputTokens"] = request.max_tokens
        if generation_config:
            payload["generationConfig"] = generation_config

        url = f"{self._base_url}/models/{provider_model}:streamGenerateContent?alt=sse&key={self._api_key}"
        response_id = f"gemini-stream-{int(time.time())}"
        created = int(time.time())

        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream("POST", url, json=payload) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise ProviderError(
                        f"Gemini error: {body.decode()}", status_code=resp.status_code
                    )

                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[len("data:") :].strip()
                    if data_str == "[DONE]":
                        break

                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    candidates = data.get("candidates", [])
                    if not candidates:
                        continue

                    c = candidates[0]
                    content_data = c.get("content", {})
                    parts = content_data.get("parts", [])
                    delta_text = "".join(p.get("text", "") for p in parts) if parts else ""
                    
                    finish_reason = c.get("finishReason")
                    if finish_reason:
                        finish_reason = finish_reason.lower()

                    yield ChatCompletionChunk(
                        id=response_id,
                        model=request.model,
                        provider=self.name,
                        created=created,
                        choices=[
                            ChatCompletionChunkChoice(
                                index=c.get("index", 0),
                                delta=ChatCompletionChunkDelta(
                                    role="assistant",
                                    content=delta_text,
                                ),
                                finish_reason=finish_reason,
                            )
                        ],
                    )

    async def health_check(self, provider_model: str, timeout: float) -> ProbeResult:
        if not self._api_key:
            return ProbeResult(success=False, latency_ms=0.0, error="API key not configured")

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                url = f"{self._base_url}/models/{provider_model}?key={self._api_key}"
                resp = await client.get(url)
            latency_ms = (time.monotonic() - start) * 1000
            if resp.status_code == 200:
                return ProbeResult(success=True, latency_ms=latency_ms)
            if resp.status_code == 404:
                return ProbeResult(
                    success=False, latency_ms=latency_ms,
                    error=f"model '{provider_model}' not found",
                )
            return ProbeResult(
                success=False, latency_ms=latency_ms, error=f"HTTP {resp.status_code}: {resp.text}"
            )
        except httpx.HTTPError as e:
            latency_ms = (time.monotonic() - start) * 1000
            return ProbeResult(success=False, latency_ms=latency_ms, error=str(e))
