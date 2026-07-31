"""
Hook point called once a streamed response finishes (successfully or with
an error), with the fully assembled content.

This is intentionally just structured logging - it is NOT the
observability stack (OpenTelemetry tracing / Prometheus metrics /
Grafana dashboards), which is a later, separate step. Its job right now
is only to give that later step a single, already-wired call site to
attach to, instead of threading accumulation logic through the router.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("llm_gateway.stream")


def on_stream_complete(
    *,
    team: str,
    model: str,
    provider: str,
    content: str,
    finish_reason: str | None,
) -> None:
    logger.info(
        "stream_complete team=%s model=%s provider=%s finish_reason=%s content_chars=%d",
        team,
        model,
        provider,
        finish_reason,
        len(content),
    )
