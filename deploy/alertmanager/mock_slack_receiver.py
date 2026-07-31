"""
Mock Slack incoming-webhook receiver (Step 16).

Alertmanager's Slack integration POSTs a JSON payload to whatever URL is
configured as the webhook and expects any 2xx response back - that's the
entire contract. This just logs whatever it receives and returns 200, so
the alerting pipeline (Prometheus -> Alertmanager -> "Slack") can be
demoed end-to-end locally without a real Slack workspace or webhook URL.
Point SLACK_WEBHOOK_URL at a real Slack incoming webhook instead to get
real notifications - nothing else in the pipeline changes.
"""
from __future__ import annotations

import json

from fastapi import FastAPI, Request

app = FastAPI(title="Mock Slack Webhook Receiver")


@app.post("/mock-slack")
async def receive(request: Request):
    body = await request.json()
    print("=" * 60)
    print("MOCK SLACK MESSAGE RECEIVED:")
    print(json.dumps(body, indent=2))
    print("=" * 60)
    return {"ok": True}
