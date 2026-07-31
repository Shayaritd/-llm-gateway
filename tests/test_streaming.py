"""
Streaming passthrough tests (Step 17): successful SSE delivery, disclaimer
appended as a trailing chunk, pre-flight fallback (before any content is
sent), and the deliberate non-fallback behavior once content has already
started flowing (see app/routers/chat.py's module docstring for why).
"""
import json

from tests.conftest import (
    TEAM_A_KEY,
    FailsMidStreamProvider,
    FailsProvider,
    WorksProvider,
    auth_headers,
    chat_payload,
)
from app.providers.registry import get_registry


def _parse_sse_lines(text: str) -> list[dict | str]:
    events = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[len("data:"):].strip()
        events.append(raw if raw == "[DONE]" else json.loads(raw))
    return events


async def test_successful_stream_delivers_chunks_and_done(gateway):
    async with gateway.stream(
        "POST", "/v1/chat/completions", json=chat_payload(stream=True), headers=auth_headers(TEAM_A_KEY)
    ) as resp:
        assert resp.status_code == 200
        text = "".join([chunk async for chunk in resp.aiter_text()])

    events = _parse_sse_lines(text)
    assert events[-1] == "[DONE]"
    content_events = [e for e in events if isinstance(e, dict)]
    assert any(c["choices"][0]["delta"].get("content") for c in content_events)
    assert content_events[0]["model"] == "gpt-4o-mini"
    assert content_events[0]["provider"] == "openai"


async def test_stream_disclaimer_appended_as_final_chunk(gateway):
    # team-a has a configured disclaimer in config.yaml.
    async with gateway.stream(
        "POST", "/v1/chat/completions", json=chat_payload(stream=True), headers=auth_headers(TEAM_A_KEY)
    ) as resp:
        text = "".join([chunk async for chunk in resp.aiter_text()])

    events = [e for e in _parse_sse_lines(text) if isinstance(e, dict)]
    full_text = "".join(
        c["delta"].get("content") or "" for e in events for c in e["choices"]
    )
    assert "AI system" in full_text  # from team-a's configured disclaimer text


async def test_stream_falls_back_before_first_chunk(gateway):
    get_registry()._providers["openai"] = FailsProvider("openai", status_code=503)
    get_registry()._providers["anthropic"] = WorksProvider("anthropic")

    async with gateway.stream(
        "POST", "/v1/chat/completions",
        json=chat_payload(model="gpt-4o-mini", stream=True), headers=auth_headers(TEAM_A_KEY),
    ) as resp:
        assert resp.status_code == 200
        text = "".join([chunk async for chunk in resp.aiter_text()])

    events = [e for e in _parse_sse_lines(text) if isinstance(e, dict)]
    assert events[0]["model"] == "claude-3-5-sonnet"
    assert events[0]["provider"] == "anthropic"
    assert events[0]["requested_model"] == "gpt-4o-mini"


async def test_stream_mid_failure_does_not_fall_back(gateway):
    # A provider that succeeds on its first chunk then fails must NOT
    # trigger fallback - headers/content are already committed to the
    # client. Confirmed by checking the "working" fallback's content never
    # appears, and no [DONE] terminator is sent.
    get_registry()._providers["openai"] = FailsMidStreamProvider("openai")
    get_registry()._providers["anthropic"] = WorksProvider("anthropic")

    async with gateway.stream(
        "POST", "/v1/chat/completions",
        json=chat_payload(model="gpt-4o-mini", stream=True), headers=auth_headers(TEAM_A_KEY),
    ) as resp:
        assert resp.status_code == 200  # headers already committed as 200 before the failure
        text = "".join([chunk async for chunk in resp.aiter_text()])

    assert "partial" in text  # the real content that was sent before the failure
    assert "anthropic" not in text  # fallback content must never appear
    assert "[DONE]" not in text  # stream ended in error, not a clean completion
    assert '"error"' in text  # a normalized SSE error event was sent instead
