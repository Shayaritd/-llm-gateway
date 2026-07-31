"""
Prometheus scrape endpoint. Exempt from team auth (see
app/middleware/auth.py) since Prometheus itself has no team API key -
scraping is an operational concern, same category as /health.
"""
from __future__ import annotations

from fastapi import APIRouter, Response

from app.metrics import render_metrics

router = APIRouter()


@router.get("/metrics")
async def metrics() -> Response:
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)
