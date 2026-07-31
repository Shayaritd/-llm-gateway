"""
Mock provider server for the local demo stack (Step 18).

Mimics the three real provider APIs closely enough for the gateway's
provider adapters to work against it unmodified - both non-streaming and
streaming, since the gateway's own SSE/NDJSON parsing logic is worth
demoing too, not just faked around.

Routes (one process, path-prefixed per "provider"):
  OpenAI-shaped:    POST /openai/chat/completions   GET /openai/models/{id}
  Anthropic-shaped: POST /anthropic/messages         GET /anthropic/models/{id}
  Ollama-shaped:    POST /ollama/api/chat            GET /ollama/api/tags

Demo trick: include the literal text "FAIL:<provider>" anywhere in a
message's content (e.g. "FAIL:openai tell me a joke") to make that mock
provider return a 503 for this call - lets a reviewer trigger fallback,
retries, and circuit-breaker trips on demand, live, without touching code.
"""
from __future__ import annotations

import json
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

app = FastAPI(title="Mock LLM Providers")


def _should_fail(body: dict, provider: str) -> bool:
    marker = f"FAIL:{provider}"
    for m in body.get("messages", []):
        content = m.get("content", "")
        if isinstance(content, str) and marker in content:
            return True
    return False


def _last_user_text(body: dict) -> str:
    for m in reversed(body.get("messages", [])):
        if m.get("role") == "user":
            return m.get("content", "")
    return ""


# ---------------------------------------------------------------- OpenAI --
@app.post("/openai/chat/completions")
async def openai_chat(request: Request):
    body = await request.json()
    if _should_fail(body, "openai"):
        raise HTTPException(status_code=503, detail="mock openai: simulated failure")

    reply = f"[mock openai/{body.get('model')}] you said: {_last_user_text(body)!r}"

    if body.get("stream"):
        async def gen():
            words = reply.split(" ")
            for i, w in enumerate(words):
                chunk = {
                    "id": "mock-openai-1", "object": "chat.completion.chunk", "created": int(time.time()),
                    "choices": [{"index": 0, "delta": {"content": w + " "}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
            final = {
                "id": "mock-openai-1", "object": "chat.completion.chunk", "created": int(time.time()),
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(final)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    return {
        "id": "mock-openai-1", "object": "chat.completion", "created": int(time.time()),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": len(reply.split()), "total_tokens": 12 + len(reply.split())},
    }


@app.get("/openai/models/{model_id}")
async def openai_model(model_id: str):
    return {"id": model_id, "object": "model"}


# ------------------------------------------------------------- Anthropic --
@app.post("/anthropic/messages")
async def anthropic_messages(request: Request):
    body = await request.json()
    if _should_fail(body, "anthropic"):
        raise HTTPException(status_code=503, detail="mock anthropic: simulated failure")

    reply = f"[mock anthropic/{body.get('model')}] you said: {_last_user_text(body)!r}"

    if body.get("stream"):
        async def gen():
            yield f"data: {json.dumps({'type': 'message_start', 'message': {'id': 'mock-anthropic-1'}})}\n\n"
            for w in reply.split(" "):
                yield f"data: {json.dumps({'type': 'content_block_delta', 'delta': {'text': w + ' '}})}\n\n"
            yield f"data: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}})}\n\n"
            yield f"data: {json.dumps({'type': 'message_stop'})}\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    return {
        "id": "mock-anthropic-1", "type": "message", "role": "assistant",
        "content": [{"type": "text", "text": reply}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 12, "output_tokens": len(reply.split())},
    }


@app.get("/anthropic/models/{model_id}")
async def anthropic_model(model_id: str):
    return {"id": model_id, "type": "model"}


# ----------------------------------------------------------------- Ollama --
@app.post("/ollama/api/chat")
async def ollama_chat(request: Request):
    body = await request.json()
    if _should_fail(body, "ollama"):
        raise HTTPException(status_code=503, detail="mock ollama: simulated failure")

    reply = f"[mock ollama/{body.get('model')}] you said: {_last_user_text(body)!r}"

    if body.get("stream", True):
        async def gen():
            for w in reply.split(" "):
                yield json.dumps({"message": {"role": "assistant", "content": w + " "}, "done": False}) + "\n"
            yield json.dumps({
                "message": {"role": "assistant", "content": ""}, "done": True,
                "prompt_eval_count": 12, "eval_count": len(reply.split()),
            }) + "\n"
        return StreamingResponse(gen(), media_type="application/x-ndjson")

    return {
        "message": {"role": "assistant", "content": reply}, "done": True,
        "prompt_eval_count": 12, "eval_count": len(reply.split()),
    }


@app.get("/ollama/api/tags")
async def ollama_tags():
    return {"models": [{"name": "llama3:latest"}]}
