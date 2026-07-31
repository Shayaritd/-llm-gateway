"""
Heuristic token estimation.

This is NOT a real tokenizer - every provider tokenizes differently, and
true counts are only known once the provider responds (Usage on the
non-streaming path). It exists purely for pre-admission / in-flight
estimates where an exact count isn't available yet:
  - TPM rate limiting (Step 5) needs a cost to deduct *before* the call.
  - Streaming budget accounting (Step 6) needs an estimate since usage
    isn't reliably available on every provider's stream.

Rule of thumb: ~4 characters per token, a common rough approximation for
English text.
"""
from __future__ import annotations

from app.schemas import ChatCompletionRequest

CHARS_PER_TOKEN = 4
DEFAULT_COMPLETION_TOKENS = 256  # reserved when the caller doesn't set max_tokens


def estimate_prompt_tokens(request: ChatCompletionRequest) -> int:
    chars = sum(len(m.content) for m in request.messages)
    return max(1, chars // CHARS_PER_TOKEN)


def estimate_completion_tokens(request: ChatCompletionRequest) -> int:
    return request.max_tokens or DEFAULT_COMPLETION_TOKENS


def estimate_text_tokens(text: str) -> int:
    return max(0, len(text) // CHARS_PER_TOKEN)
