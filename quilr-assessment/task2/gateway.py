"""
Task 2 — MCP Security Gateway Proxy

Sits between an AI agent and a downstream MCP server.
Extracts role from a signed JWT, then:
  - tools/list  → always forward
  - tools/call  → block admin_* tools if role != "admin"
"""
import os
import logging
import httpx
import jwt  # PyJWT
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

DOWNSTREAM_URL: str = os.environ.get(
    "DOWNSTREAM_MCP_URL", "http://localhost:8001/mcp"
)
JWT_SECRET: str = os.environ.get("GATEWAY_JWT_SECRET", "dev-secret-change-in-prod")
JWT_ALG: str = "HS256"

app = FastAPI(title="MCP Security Gateway", version="1.0.0")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rpc_error(rpc_id, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": code, "message": message},
    }


def _extract_role(authorization: str | None) -> str | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
        return payload.get("role")
    except jwt.ExpiredSignatureError:
        log.warning("Rejected expired JWT")
    except jwt.PyJWTError as exc:
        log.warning("JWT validation error: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Proxy endpoint
# ---------------------------------------------------------------------------

@app.post("/mcp")
async def mcp_proxy(request: Request) -> Response:
    role = _extract_role(request.headers.get("authorization"))
    if role is None:
        return JSONResponse(
            status_code=401,
            content={"error": "Missing or invalid Bearer token"},
        )

    raw = await request.body()
    try:
        body: dict = __import__("json").loads(raw)
    except Exception:
        return JSONResponse(
            status_code=400,
            content=_rpc_error(None, -32700, "Parse error"),
        )

    rpc_id = body.get("id")
    method: str = body.get("method", "")
    params: dict = body.get("params") or {}

    # Authorization check on tool invocations
    if method == "tools/call":
        tool_name: str = params.get("name", "")
        if tool_name.startswith("admin_") and role != "admin":
            log.warning(
                "BLOCKED id=%s role=%s attempted %s", rpc_id, role, tool_name
            )
            return JSONResponse(
                content=_rpc_error(rpc_id, -32001, "Unauthorized Tool Call"),
            )

    log.info("FORWARD id=%s method=%s role=%s", rpc_id, method, role)

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(
                DOWNSTREAM_URL,
                content=raw,
                headers={"Content-Type": "application/json"},
            )
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type="application/json",
            )
        except httpx.RequestError as exc:
            log.error("Downstream unreachable: %s", exc)
            return JSONResponse(
                status_code=502,
                content=_rpc_error(rpc_id, -32603, "Downstream server unreachable"),
            )
