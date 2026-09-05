"""
Task 3 — LLM Gateway Streaming Guardrail (PII Redaction)

Proxies requests to an OpenAI-compatible endpoint and streams the response
back with PII redacted in real time.

Key design decisions:
 - Operates at the SSE frame level (parses each "data: {...}" line).
 - Maintains a _PIIBuffer with a lookahead window.  Before flushing, it
   scans for partial matches at the cut boundary and pulls the cut back to
   avoid splitting a PII token across two consecutive output chunks.
 - The httpx stream context lives inside the async generator, keeping the
   upstream connection open for the full response duration.
"""
import os
import re
import json
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

UPSTREAM_URL: str = os.environ.get(
    "LLM_UPSTREAM_URL", "https://api.openai.com/v1/chat/completions"
)
UPSTREAM_API_KEY: str = os.environ.get("UPSTREAM_API_KEY", "")

# How many chars to hold back as a baseline lookahead window.
_LOOKAHEAD = 80

_PII_RULES: list[tuple[re.Pattern, str]] = [
    (
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        "[REDACTED_EMAIL]",
    ),
    (
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "[REDACTED_SSN]",
    ),
    (
        re.compile(r"\b(?:\d{4}[\s\-]?){3}\d{4}\b"),
        "[REDACTED_CC]",
    ),
]

# ---------------------------------------------------------------------------
# PII buffer
# ---------------------------------------------------------------------------

def _apply_rules(text: str) -> str:
    for pattern, replacement in _PII_RULES:
        text = pattern.sub(replacement, text)
    return text


def _safe_cut(buf: str, desired: int) -> int:
    """
    Retreat `desired` so the cut does not bisect a complete PII match.

    Scans the full buffer for each pattern.  If a match starts before `desired`
    and ends at or after `desired`, the cut would split the token — we pull back
    to just before the match start.
    """
    if desired <= 0:
        return 0
    for pattern, _ in _PII_RULES:
        pos = 0
        while pos < desired:
            m = pattern.search(buf, pos)
            if not m:
                break
            if m.start() < desired <= m.end():
                desired = min(desired, m.start())
                if desired == 0:
                    return 0
            pos = m.end()
    return desired


class _PIIBuffer:
    """
    Streaming PII redaction buffer.

    Defers flushing the last _LOOKAHEAD characters and additionally ensures
    the flush boundary does not bisect a partial PII token.
    """

    __slots__ = ("_buf",)

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, text: str) -> str:
        self._buf += text
        cut = _safe_cut(self._buf, max(0, len(self._buf) - _LOOKAHEAD))
        safe, self._buf = self._buf[:cut], self._buf[cut:]
        return _apply_rules(safe) if safe else ""

    def flush(self) -> str:
        out = _apply_rules(self._buf)
        self._buf = ""
        return out


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def _redact_delta(line: str, buf: _PIIBuffer) -> str:
    try:
        payload = json.loads(line[6:])
        for choice in payload.get("choices", []):
            delta = choice.get("delta", {})
            raw = delta.get("content")
            if isinstance(raw, str) and raw:
                delta["content"] = buf.feed(raw)
        return "data: " + json.dumps(payload, separators=(",", ":"))
    except (json.JSONDecodeError, KeyError):
        return line


def _synthetic_chunk(content: str) -> str:
    chunk = {
        "id": "guardrail-tail",
        "object": "chat.completion.chunk",
        "choices": [
            {"index": 0, "delta": {"content": content}, "finish_reason": None}
        ],
    }
    return "data: " + json.dumps(chunk, separators=(",", ":"))


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="LLM Streaming Guardrail", version="1.0.0", lifespan=lifespan)


@app.post("/v1/chat/completions")
async def completions(request: Request) -> StreamingResponse:
    body = await request.json()
    body["stream"] = True

    upstream_headers: dict[str, str] = {"Content-Type": "application/json"}
    if UPSTREAM_API_KEY:
        upstream_headers["Authorization"] = f"Bearer {UPSTREAM_API_KEY}"

    async def generate():
        buf = _PIIBuffer()
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                UPSTREAM_URL,
                json=body,
                headers=upstream_headers,
            ) as upstream:
                if upstream.status_code != 200:
                    error_body = await upstream.aread()
                    log.error("Upstream %s: %s", upstream.status_code, error_body[:256])
                    yield f"data: {json.dumps({'error': 'upstream_error', 'status': upstream.status_code})}\n\n"
                    return

                async for line in upstream.aiter_lines():
                    if not line:
                        continue

                    if line == "data: [DONE]":
                        tail = buf.flush()
                        if tail:
                            yield _synthetic_chunk(tail) + "\n\n"
                        yield "data: [DONE]\n\n"
                        return

                    if line.startswith("data: "):
                        yield _redact_delta(line, buf) + "\n\n"
                    else:
                        yield line + "\n"

                # Stream ended without explicit [DONE]
                tail = buf.flush()
                if tail:
                    yield _synthetic_chunk(tail) + "\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
