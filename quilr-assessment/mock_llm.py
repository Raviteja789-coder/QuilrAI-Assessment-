"""
Mock LLM server — used to test tasks 3 and 4 without real API keys.

Streams a fake SSE response that intentionally contains PII so the
task-3 guardrail redaction can be verified.

Run:  uvicorn mock_llm:app --port 11434
"""
import asyncio
import json
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

app = FastAPI(title="Mock LLM")

_CHUNKS = [
    "Sure! ",
    "The customer email is ",
    "john.smith@acm",          # deliberately split mid-pattern
    "e.org and their ",
    "SSN is 123-45-",          # SSN split across chunks
    "6789. ",
    "Card on file: 4111 1111 1111 ",  # CC split across chunks
    "1111. ",
    "Have a great day!",
]


async def _fake_stream():
    for i, text in enumerate(_CHUNKS):
        chunk = {
            "id": f"chatcmpl-mock-{i}",
            "object": "chat.completion.chunk",
            "choices": [
                {"index": 0, "delta": {"content": text}, "finish_reason": None}
            ],
        }
        yield f"data: {json.dumps(chunk)}\n\n"
        await asyncio.sleep(0.05)
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def completions(request: Request):
    body = await request.json()
    if body.get("stream", False):
        return StreamingResponse(_fake_stream(), media_type="text/event-stream")
    # Non-streaming fallback (used by task 4 proxy tests)
    return JSONResponse({
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Mock response — no real LLM called.",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    })
