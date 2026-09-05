"""
Task 4 — Rate-Limiting & Model Fallback Router

Sliding-window token rate limiter (50k tokens/min per tenant) backed by
SQLite via aiosqlite.  Primary LLM endpoint tried first; on 429 or timeout
(3 s) the request is transparently retried against a fallback provider.
All error responses are sanitised — no upstream stack traces leak through.
"""
import os
import time
import json
import logging
import asyncio
from contextlib import asynccontextmanager

import aiosqlite
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

DB_PATH: str = os.environ.get("RATE_LIMITER_DB", "rate_limiter.db")
WINDOW_SECS: int = 60
TOKEN_LIMIT: int = 50_000
CALL_TIMEOUT: float = 3.0  # seconds

PRIMARY_URL: str = os.environ.get(
    "PRIMARY_LLM_URL", "https://api.openai.com/v1/chat/completions"
)
PRIMARY_KEY: str = os.environ.get("PRIMARY_API_KEY", "")
FALLBACK_URL: str = os.environ.get(
    "FALLBACK_LLM_URL", "https://api.anthropic.com/v1/messages"
)
FALLBACK_KEY: str = os.environ.get("FALLBACK_API_KEY", "")


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_events (
    tenant  TEXT    NOT NULL,
    ts      REAL    NOT NULL,
    tokens  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tenant_ts ON rate_events (tenant, ts);
"""


async def _init_db(db: aiosqlite.Connection) -> None:
    for stmt in _SCHEMA.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            await db.execute(stmt)
    await db.commit()


async def _check_and_record(
    db: aiosqlite.Connection, tenant: str, tokens: int
) -> tuple[bool, int]:
    """
    Atomically check whether tenant is within limits and, if so, record usage.
    Returns (allowed, current_window_total).
    """
    now = time.time()
    window_start = now - WINDOW_SECS

    # Evict stale entries first
    await db.execute(
        "DELETE FROM rate_events WHERE tenant = ? AND ts < ?",
        (tenant, window_start),
    )

    cursor = await db.execute(
        "SELECT COALESCE(SUM(tokens), 0) FROM rate_events WHERE tenant = ? AND ts >= ?",
        (tenant, window_start),
    )
    row = await cursor.fetchone()
    used: int = row[0] if row else 0

    if used + tokens > TOKEN_LIMIT:
        await db.commit()
        return False, used

    await db.execute(
        "INSERT INTO rate_events (tenant, ts, tokens) VALUES (?, ?, ?)",
        (tenant, now, tokens),
    )
    await db.commit()
    return True, used + tokens


def _estimate_tokens(body: dict) -> int:
    """Rough 4-chars-per-token heuristic on the full request payload."""
    return max(1, len(json.dumps(body)) // 4)


# ---------------------------------------------------------------------------
# Gateway error schema — never exposes internal details
# ---------------------------------------------------------------------------

def _gw_error(code: str, message: str, status: int = 500) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
    )


# ---------------------------------------------------------------------------
# LLM call with timeout
# ---------------------------------------------------------------------------

async def _call_llm(
    client: httpx.AsyncClient,
    url: str,
    api_key: str,
    body: dict,
) -> httpx.Response:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return await asyncio.wait_for(
        client.post(url, json=body, headers=headers),
        timeout=CALL_TIMEOUT,
    )


def _proxy_response(resp: httpx.Response) -> JSONResponse:
    try:
        content = resp.json()
    except Exception:
        content = {"raw": resp.text[:512]}
    return JSONResponse(status_code=resp.status_code, content=content)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    db = await aiosqlite.connect(DB_PATH)
    await _init_db(db)
    app.state.db = db
    log.info("Rate-limiter DB ready at %s", DB_PATH)
    yield
    await db.close()


app = FastAPI(
    title="LLM Rate-Limiting Router",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Routing endpoint
# ---------------------------------------------------------------------------

@app.post("/v1/chat/completions")
async def completions(request: Request) -> JSONResponse:
    tenant: str = request.headers.get("x-tenant-id", "default")

    try:
        body = await request.json()
    except Exception:
        return _gw_error("bad_request", "Request body is not valid JSON", status=400)

    tokens = _estimate_tokens(body)
    allowed, window_used = await _check_and_record(request.app.state.db, tenant, tokens)

    if not allowed:
        log.warning(
            "Rate limit hit: tenant=%s window_used=%d tokens=%d", tenant, window_used, tokens
        )
        return _gw_error(
            "rate_limit_exceeded",
            f"Token quota exceeded ({window_used}/{TOKEN_LIMIT} tokens used in the last {WINDOW_SECS}s). "
            "Retry after the window resets.",
            status=429,
        )

    async with httpx.AsyncClient() as client:
        # --- Primary ---
        try:
            resp = await _call_llm(client, PRIMARY_URL, PRIMARY_KEY, body)
            if resp.status_code != 429:
                log.info("Primary OK: tenant=%s status=%s", tenant, resp.status_code)
                return _proxy_response(resp)
            log.warning("Primary returned 429; falling over to secondary")
        except asyncio.TimeoutError:
            log.warning("Primary timed out after %.1fs; falling over", CALL_TIMEOUT)
        except httpx.RequestError as exc:
            log.warning("Primary request error (%s); falling over", type(exc).__name__)

        # --- Fallback ---
        try:
            resp = await _call_llm(client, FALLBACK_URL, FALLBACK_KEY, body)
            log.info("Fallback OK: tenant=%s status=%s", tenant, resp.status_code)
            return _proxy_response(resp)
        except asyncio.TimeoutError:
            log.error("Fallback also timed out after %.1fs", CALL_TIMEOUT)
        except httpx.RequestError as exc:
            log.error("Fallback request error: %s", exc)

    return _gw_error(
        "service_unavailable",
        "All model endpoints are currently unavailable. Please try again shortly.",
        status=503,
    )
